from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.main import create_app
from app.users import LoginThrottle, hash_password, verify_password

from .conftest import ADMIN_PASSWORD, login, make_settings, pdf_bytes, wait_for


def test_password_hashing():
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong horse", stored)
    assert not verify_password("x", "garbage")
    assert hash_password("same") != hash_password("same")  # salted


def test_throttle_grows_after_free_attempts():
    t = LoginThrottle()
    for _ in range(5):
        assert t.delay("ip:1") == 0
        t.failed("ip:1")
    assert t.delay("ip:1") == 1
    t.failed("ip:1")
    assert t.delay("ip:1") == 2
    for _ in range(20):
        t.failed("ip:1")
    assert t.delay("ip:1") == LoginThrottle.MAX_DELAY
    t.succeeded("ip:1")
    assert t.delay("ip:1") == 0


def test_first_run_setup(tmp_path, printers, scanners):
    app = create_app(make_settings(tmp_path, password=""), printers, scanners)
    with TestClient(app) as c:
        r = c.get("/print", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/setup"
        assert c.get("/api/jobs").status_code == 503
        assert c.get("/healthz").status_code == 200

        bad = c.post("/setup", data={"username": "boss", "password": "short",
                                     "password2": "short"})
        assert bad.status_code == 400
        r = c.post("/setup", data={"username": "Boss", "password": "long-enough-1",
                                   "password2": "long-enough-1", "email": "b@example.com"},
                   follow_redirects=False)
        assert r.status_code == 303
        me = c.get("/api/me").json()
        assert me["username"] == "boss" and me["role"] == "admin"
        # Setup is closed now.
        assert c.get("/setup", follow_redirects=False).headers["location"] == "/"
        assert c.post("/setup", data={"username": "evil", "password": "x" * 10,
                                      "password2": "x" * 10},
                      follow_redirects=False).status_code == 303
        assert [u["username"] for u in c.get("/api/users").json()] == ["boss"]


def test_login_logout_and_bad_credentials(anon_client):
    c = anon_client
    assert c.get("/api/jobs").status_code == 401
    r = c.get("/jobs", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert c.post("/login", data={"username": "admin", "password": "nope"}).status_code == 401
    assert c.post("/login", data={"username": "ghost", "password": "nope"}).status_code == 401
    r = c.post("/login", data={"username": "ADMIN", "password": ADMIN_PASSWORD,
                               "next": "//evil.example"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/api/me").json()["username"] == "admin"
    c.post("/logout")
    assert c.get("/api/me").status_code == 401


def _make_user(client, name="alice", password="alice-pass-1", **extra):
    r = client.post("/api/users", json={"username": name, "password": password, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def test_roles_and_job_visibility(client):
    _make_user(client)
    admin_job = client.post("/api/print", files={"file": ("a.pdf", pdf_bytes(1))},
                            data={"printer": "Fake_Printer"}).json()["id"]
    wait_for(client, admin_job)

    client.post("/logout")
    login(client, "alice", "alice-pass-1")
    assert client.get("/api/me").json()["role"] == "user"
    assert client.get("/api/jobs").json() == []
    assert client.get(f"/api/jobs/{admin_job}").status_code == 404
    assert client.post(f"/api/jobs/{admin_job}/cancel").status_code == 404
    assert client.get("/api/users").status_code == 403
    assert client.post("/api/printers/sync").status_code == 403
    assert client.get("/admin/users").status_code == 403
    assert client.post("/devices/refresh").status_code == 403

    mine = client.post("/api/print", files={"file": ("b.pdf", pdf_bytes(1))},
                       data={"printer": "Fake_Printer"}).json()
    assert mine["user"] == "alice"
    assert [j["id"] for j in client.get("/api/jobs").json()] == [mine["id"]]
    wait_for(client, mine["id"])

    client.post("/logout")
    login(client)
    assert {j["user"] for j in client.get("/api/jobs").json()} == {"admin", "alice"}
    assert "<th>User</th>" in client.get("/jobs").text


def test_printer_permissions(client, printers):
    printers.ensure_queue("Second", "ipp://second/ipp/print", "Second Printer")
    _make_user(client, allowed_printers=["Second"])
    client.post("/logout")
    login(client, "alice", "alice-pass-1")
    assert [p["name"] for p in client.get("/api/printers").json()] == ["Second"]
    r = client.post("/api/print", files={"file": ("a.pdf", pdf_bytes(1))},
                    data={"printer": "Fake_Printer"})
    assert r.status_code == 403


def test_api_token(client):
    r = client.post("/account/token", data={"action": "new"})
    token = r.text.split("<pre><code>")[1].split("</code>")[0]
    assert token.startswith("plt_")
    fresh = TestClient(client.app)
    headers = {"Authorization": f"Bearer {token}"}
    assert fresh.get("/api/me", headers=headers).json()["username"] == "admin"
    assert fresh.get("/api/me", headers={"Authorization": "Bearer plt_wrong"}).status_code == 401
    client.post("/account/token", data={"action": "revoke"})
    assert fresh.get("/api/me", headers=headers).status_code == 401


def test_password_change_invalidates_other_sessions(client):
    other = TestClient(client.app)
    login(other)
    r = client.post("/account/password", data={"current": "wrong", "new": "new-pass-123",
                                               "new2": "new-pass-123"})
    assert r.status_code == 403
    r = client.post("/account/password", data={"current": ADMIN_PASSWORD,
                                                "new": "new-pass-123", "new2": "new-pass-123"})
    assert r.status_code == 200
    assert client.get("/api/me").status_code == 200        # this session survives
    assert other.get("/api/me").status_code == 401         # the other one is logged out
    login(other, "admin", "new-pass-123")


def test_deactivated_user_is_logged_out(client):
    alice = _make_user(client)
    other = TestClient(client.app)
    login(other, "alice", "alice-pass-1")
    assert client.patch(f"/api/users/{alice['id']}", json={"active": False}).status_code == 200
    assert other.get("/api/me").status_code == 401
    r = other.post("/login", data={"username": "alice", "password": "alice-pass-1"})
    assert r.status_code == 401


def test_last_admin_is_protected(client):
    me = client.get("/api/me").json()
    assert client.patch(f"/api/users/{me['id']}", json={"role": "user"}).status_code == 409
    assert client.patch(f"/api/users/{me['id']}", json={"active": False}).status_code == 409
    second = _make_user(client, "root2", "root2-pass-1", role="admin")
    assert client.patch(f"/api/users/{second['id']}", json={"role": "user"}).status_code == 200


def test_user_validation(client):
    assert client.post("/api/users", json={"username": "a", "password": "x" * 10}
                       ).status_code == 400
    assert client.post("/api/users", json={"username": "bad name", "password": "x" * 10}
                       ).status_code == 400
    assert client.post("/api/users", json={"username": "bob", "password": "short"}
                       ).status_code == 400
    assert client.post("/api/users", json={"username": "admin", "password": "x" * 10}
                       ).status_code == 409
    assert client.post("/api/users", json={"username": "bob", "password": "x" * 10,
                                           "email": "nope"}).status_code == 400


def test_admin_ui_create_and_edit(client):
    r = client.post("/admin/users", data={"username": "carol", "password": "carol-pass-1",
                                          "role": "user", "email": "c@example.com"},
                    follow_redirects=False)
    assert r.status_code == 303
    carol = next(u for u in client.get("/api/users").json() if u["username"] == "carol")
    r = client.post(f"/admin/users/{carol['id']}",
                    data={"role": "user", "email": "c2@example.com", "active": "1",
                          "allowed_printers": "Fake_Printer"})
    assert r.status_code == 200 and "Saved" in r.text
    carol = next(u for u in client.get("/api/users").json() if u["username"] == "carol")
    assert carol["email"] == "c2@example.com" and carol["allowed_printers"] == ["Fake_Printer"]


def test_upgrade_from_v01_database(tmp_path, printers, scanners):
    """A v0.1 database (jobs, no users) keeps its jobs and gets the bootstrap admin."""
    db_path = tmp_path / "platen.db"
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE job (id VARCHAR PRIMARY KEY, kind VARCHAR, status VARCHAR, user VARCHAR,
          device VARCHAR, device_label VARCHAR, filename VARCHAR, file_path VARCHAR,
          file_size INTEGER, pages INTEGER, copies INTEGER, page_range VARCHAR, duplex VARCHAR,
          color_mode VARCHAR, dpi INTEGER, cups_job_id INTEGER, error VARCHAR,
          created_at DATETIME, started_at DATETIME, finished_at DATETIME, files_purged BOOLEAN);
        CREATE TABLE scanpage (id VARCHAR PRIMARY KEY, job_id VARCHAR, position INTEGER,
          path VARCHAR, thumb_path VARCHAR, created_at DATETIME);
        INSERT INTO job VALUES ('oldjob000001', 'scan', 'done', 'admin', 'airscan:x', 'HP',
          'scan.pdf', NULL, 1000, 2, 1, NULL, NULL, 'Gray', 200, NULL, NULL,
          '2026-11-23 14:17:55.515735', '2026-11-23 14:17:55.515515',
          '2026-11-23 14:18:14.469039', 0);
    """)
    con.commit()
    con.close()
    app = create_app(make_settings(tmp_path, database_url=""), printers, scanners)
    with TestClient(app) as c:
        login(c)
        jobs = c.get("/api/jobs").json()
        assert [j["id"] for j in jobs] == ["oldjob000001"]
        assert c.get("/jobs/oldjob000001").status_code == 200
