from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import quotas, stats
from app.main import create_app
from app.models import Job

from .conftest import login, make_settings, pdf_bytes, scan_and_finish, wait_for


def _job(**kw) -> Job:
    now = datetime.now(timezone.utc)
    base = dict(kind="print", status="done", device="P", device_label="Printer A", copies=1,
                created_at=now, user="admin")
    base.update(kw)
    return Job(**base)


# --- pure computations -----------------------------------------------------------

def test_sheets_and_duplex():
    assert stats.sheets_for(_job(pages=5, copies=2)) == (10, 10)
    assert stats.sheets_for(_job(pages=5, copies=2, duplex="two-sided-long-edge")) == (10, 6)
    assert stats.sheets_for(_job(pages=None)) == (0, 0)


def test_compute_totals_and_buckets():
    key, start, end = stats.resolve_range("7d")
    today = datetime.now(timezone.utc)
    jobs = [
        _job(pages=4, copies=1, duplex="two-sided-long-edge", color_mode="monochrome"),
        _job(pages=3, copies=2, user="alice", device_label="Printer B"),
        _job(kind="scan", pages=2, user="alice"),
        _job(status="failed", error="CUPS aborted the job (printer-stopped)"),
        _job(status="failed", error="CUPS aborted the job (printer-stopped)"),
        _job(status="cancelled"),
        _job(pages=None),                                    # text file, unknown pages
        _job(pages=9, created_at=today - timedelta(days=3)),
    ]
    st = stats.compute(jobs, key, start, end)
    assert st.unit == "day" and len(st.series) == 7
    assert st.printed_pages == 4 + 6 + 9
    assert st.sheets == 2 + 6 + 9 and st.sheets_saved == 2
    assert (st.duplex_pages, st.simplex_pages) == (4, 15)
    assert (st.mono_pages, st.default_color_pages) == (4, 15)
    assert st.scanned_pages == 2 and st.scan_jobs == 1
    assert (st.failed_jobs, st.cancelled_jobs, st.finished_jobs) == (2, 1, 8)
    assert st.unknown_page_jobs == 1
    assert st.top_errors == [{"error": "CUPS aborted the job (printer-stopped)", "count": 2}]
    assert st.by_printer[0] == {"name": "Printer A", "pages": 13}
    assert st.by_user[0]["user"] == "admin" and st.by_user[0]["printed"] == 13
    assert sum(b["printed"] for b in st.series) == 19
    assert st.series[-1]["printed"] == 10 and st.series[-1]["scanned"] == 2


def test_ranges_and_month_buckets():
    now = datetime(2026, 11, 23, 15, 0).astimezone()
    key, start, _ = stats.resolve_range("mtd", now)
    assert key == "mtd" and start.day == 1 and start.hour == 0
    key, start, end = stats.resolve_range("365d", now)
    assert stats.bucket_unit(start, end) == "month"
    assert len(stats.bucket_keys(start, end, "month")) in (12, 13)
    assert stats.resolve_range("bogus", now)[0] == "30d"


def test_period_start():
    wed = datetime(2026, 11, 25, 15, 30).astimezone()
    week = quotas.period_start("week", wed).astimezone()
    assert (week.weekday(), week.day, week.hour) == (0, 23, 0)
    month = quotas.period_start("month", wed).astimezone()
    assert (month.day, month.hour) == (1, 0)


# --- quotas end to end -------------------------------------------------------------

def _user_with_quota(client, pages, period="month", name="alice"):
    r = client.post("/api/users", json={"username": name, "password": "alice-pass-1",
                                        "quota_pages": pages, "quota_period": period})
    assert r.status_code == 201, r.text
    client.post("/logout")
    login(client, name, "alice-pass-1")


def _print(client, pages=1, copies=1):
    return client.post("/api/print", files={"file": ("a.pdf", pdf_bytes(pages))},
                       data={"printer": "Fake_Printer", "copies": str(copies)})


def test_quota_blocks_jobs_over_the_limit(client):
    _user_with_quota(client, 5)
    first = _print(client, pages=3)
    assert wait_for(client, first.json()["id"])["status"] == "done"
    assert "2 of 5 pages left this month" in client.get("/print").text

    too_big = _print(client, pages=2, copies=2)     # needs 4, only 2 left
    job = wait_for(client, too_big.json()["id"])
    assert job["status"] == "failed" and "only 2 of your 5 pages" in job["error"]

    ok = _print(client, pages=2)
    assert wait_for(client, ok.json()["id"])["status"] == "done"
    me = client.get("/api/me").json()["quota"]
    assert me == {"limit": 5, "period": "month", "used": 5, "remaining": 0}

    r = _print(client, pages=1)                       # used up: refused at upload
    assert r.status_code == 403 and "used up" in r.json()["detail"]
    assert "is used up" in client.get("/print").text


def test_quota_warn_mode_allows(tmp_path, printers, scanners):
    app = create_app(make_settings(tmp_path, quota_mode="warn"), printers, scanners)
    with TestClient(app) as c:
        login(c)
        _user_with_quota(c, 1)
        r = _print(c, pages=3)
        assert wait_for(c, r.json()["id"])["status"] == "done"
        assert c.get("/api/me").json()["quota"]["remaining"] == -2


def test_admin_sets_quota_in_ui(client):
    client.post("/admin/users", data={"username": "bob", "password": "bob-pass-12",
                                      "role": "user", "quota_pages": "50",
                                      "quota_period": "week"})
    bob = next(u for u in client.get("/api/users").json() if u["username"] == "bob")
    assert (bob["quota_pages"], bob["quota_period"]) == (50, "week")
    r = client.post(f"/admin/users/{bob['id']}", data={"role": "user", "active": "1",
                                                       "quota_pages": "-1"})
    assert r.status_code == 400
    assert "0 / 50 per week" in client.get("/admin/users").text


# --- dashboard ---------------------------------------------------------------------

def test_stats_page_and_api(client):
    wait_for(client, _print(client, pages=2, copies=3).json()["id"])
    wait_for(client, scan_and_finish(client, pages=2))
    data = client.get("/api/stats?range=7d").json()
    assert data["totals"]["printed_pages"] == 6 and data["totals"]["scanned_pages"] == 2
    assert data["series"][-1] == {**data["series"][-1], "printed": 6, "scanned": 2}
    html = client.get("/stats").text
    assert "pages printed" in html and 'id="chart-data"' in html and "Show as table" in html
    for r in ("7d", "90d", "365d", "mtd"):
        assert client.get(f"/stats?range={r}").status_code == 200
    assert client.get("/static/charts.js").status_code == 200


def test_users_only_see_their_own_stats(client):
    wait_for(client, _print(client, pages=4).json()["id"])
    client.post("/api/users", json={"username": "carol", "password": "carol-pass-1"})
    client.post("/logout")
    login(client, "carol", "carol-pass-1")
    assert client.get("/api/stats").json()["totals"]["printed_pages"] == 0
    assert client.get("/api/stats?user=admin").json()["totals"]["printed_pages"] == 0
    assert client.get("/api/stats").json()["by_user"] == []
    assert "All users" not in client.get("/stats").text


def test_supply_sampling(client):
    services = client.app.state.services
    assert asyncio.run(services.sample_supplies()) == 1
    history = client.get("/api/supplies").json()
    assert history[0]["marker"] == "Black toner" and history[0]["points"][0]["level"] == 72
    assert "Toner level" in client.get("/stats").text
    assert services.latest_supplies()["Fake_Printer"][0].level == 72


def test_migration_from_v02(tmp_path, printers, scanners):
    """A v0.2 database (users without quota columns, schema version 0) is upgraded."""
    db = tmp_path / "platen.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR UNIQUE, password_hash VARCHAR,
          role VARCHAR, email VARCHAR, paperless_token VARCHAR, allowed_printers VARCHAR,
          api_token_hash VARCHAR, session_epoch INTEGER, active BOOLEAN, created_at DATETIME,
          last_login_at DATETIME);
        CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER);
        INSERT INTO schema_version VALUES (1, 0);
    """)
    from app.users import hash_password
    con.execute("INSERT INTO users VALUES (1, 'admin', ?, 'admin', '', '', '', '', 0, 1, "
                "'2026-11-23 15:00:00', NULL)", (hash_password("old-admin-pw"),))
    con.commit()
    con.close()
    app = create_app(make_settings(tmp_path, password=""), printers, scanners)
    with TestClient(app) as c:
        login(c, "admin", "old-admin-pw")
        me = c.get("/api/me").json()
        assert me["quota_pages"] == 0 and me["quota_period"] == "month"
    con = sqlite3.connect(db)
    assert con.execute("SELECT version FROM schema_version").fetchone() == (1,)
    con.close()
