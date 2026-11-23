from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from app.main import create_app
from app.security import RateLimiter, cross_origin_problem

from .conftest import ADMIN_PASSWORD, login, make_settings, pdf_bytes, scan_and_finish


def _app_client(tmp_path, printers, scanners, **overrides):
    return TestClient(create_app(make_settings(tmp_path, **overrides), printers, scanners))


# --- security headers --------------------------------------------------------------

def test_security_headers(client):
    for path in ("/", "/api/me", "/static/app.css"):
        r = client.get(path)
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "same-origin"
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-eval" not in csp
    # The interactive API docs load assets from a CDN, so they only get frame-ancestors.
    assert client.get("/docs").headers["content-security-policy"] == "frame-ancestors 'none'"


INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)[^>]*>", re.I)
INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)


def test_pages_have_no_inline_scripts(client):
    """The CSP forbids inline scripts: every page must work with script-src 'self'."""
    job_id = scan_and_finish(client)
    open_scan = client.post("/api/scan", json={"scanner": "fake:scanner"}).json()["id"]
    pages = ["/", "/print", "/scan", f"/scan/{open_scan}", "/jobs", f"/jobs/{job_id}",
             "/stats", "/devices", "/account", "/admin/users", "/admin/users/new",
             "/jobs/does-not-exist"]
    for path in pages:
        html = client.get(path).text
        assert not INLINE_SCRIPT.search(html), path
        assert not INLINE_HANDLER.search(html), (path, INLINE_HANDLER.search(html))
        assert "javascript:" not in html, path
    client.post("/logout")
    assert not INLINE_SCRIPT.search(client.get("/login").text)


# --- cross-site request blocking ---------------------------------------------------

def _h(**headers) -> Headers:
    return Headers({k.replace("_", "-"): v for k, v in headers.items()})


def test_cross_origin_rules():
    trusted = frozenset({"https://home.example.com"})
    ok = [
        ("GET", _h(sec_fetch_site="cross-site")),                      # reads are fine
        ("POST", _h()),                                                # curl / API clients
        ("POST", _h(sec_fetch_site="same-origin")),
        ("POST", _h(sec_fetch_site="none")),
        ("POST", _h(origin="http://pi.local:8631", host="pi.local:8631")),
        ("POST", _h(origin="https://home.example.com", sec_fetch_site="cross-site")),
    ]
    for method, headers in ok:
        assert cross_origin_problem(method, headers, trusted) is None, headers
    blocked = [
        ("POST", _h(sec_fetch_site="cross-site")),
        ("POST", _h(sec_fetch_site="same-site")),
        ("DELETE", _h(origin="https://evil.example", host="pi.local:8631")),
        ("POST", _h(origin="null", host="pi.local:8631")),
    ]
    for method, headers in blocked:
        assert cross_origin_problem(method, headers, trusted), headers


def test_cross_site_requests_blocked(client):
    evil = {"origin": "https://evil.example", "sec-fetch-site": "cross-site"}
    r = client.post("/api/print", files={"file": ("a.pdf", pdf_bytes(1))},
                    data={"printer": "Fake_Printer"}, headers=evil)
    assert r.status_code == 403 and "Cross-site" in r.json()["detail"]
    r = client.post("/account/logout-everywhere", headers=evil, follow_redirects=False)
    assert r.status_code == 403 and "Cross-site" in r.text
    # Login CSRF is blocked too (before authentication runs).
    r = client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD},
                    headers=evil, follow_redirects=False)
    assert r.status_code == 403
    assert client.get("/api/jobs").json() == []

    same = {"origin": "http://testserver", "sec-fetch-site": "same-origin"}
    r = client.post("/api/print", files={"file": ("a.pdf", pdf_bytes(1))},
                    data={"printer": "Fake_Printer"}, headers=same)
    assert r.status_code == 202


def test_trusted_origins(tmp_path, printers, scanners):
    with _app_client(tmp_path, printers, scanners,
                     trusted_origins="https://Home.Example.com/") as c:
        login(c)
        r = c.post("/api/printers/sync", headers={"origin": "https://home.example.com",
                                                  "sec-fetch-site": "cross-site"})
        assert r.status_code == 200


# --- body size ---------------------------------------------------------------------

def test_oversized_upload_rejected_early(tmp_path, printers, scanners):
    with _app_client(tmp_path, printers, scanners, max_upload_mb=1) as c:
        login(c)
        r = c.post("/api/print", files={"file": ("big.pdf", b"%PDF-" + b"0" * 3_000_000)},
                   data={"printer": "Fake_Printer"})
        assert r.status_code == 413 and "too large" in r.json()["detail"]
        assert c.get("/api/jobs").json() == []


# --- rate limits -------------------------------------------------------------------

def test_rate_limiter_window():
    rl = RateLimiter()
    assert all(rl.hit("k", 3, now=t) == 0 for t in (0, 1, 2))
    assert rl.hit("k", 3, now=10) == pytest.approx(50)
    assert rl.hit("other", 3, now=10) == 0
    assert rl.hit("k", 3, now=60.5) == 0          # the hit at t=0 has expired
    assert rl.hit("k", 0, now=61) == 0            # 0 = unlimited


def test_rate_limit_per_user(tmp_path, printers, scanners):
    with _app_client(tmp_path, printers, scanners, rate_limit_per_minute=8) as c:
        login(c)  # the login POST counts for the IP, not the user
        statuses = [c.get("/api/me").status_code for _ in range(10)]
        assert statuses[:8] == [200] * 8 and statuses[8:] == [429, 429]
        r = c.get("/api/me")
        assert int(r.headers["retry-after"]) > 0 and "Too many requests" in r.json()["detail"]
        assert c.get("/static/app.css").status_code == 200   # static files are not limited
        assert c.get("/healthz").status_code == 200


def test_heavy_rate_limit(tmp_path, printers, scanners):
    with _app_client(tmp_path, printers, scanners, heavy_rate_limit_per_minute=2) as c:
        login(c)
        codes = [c.post("/api/print", files={"file": ("a.pdf", pdf_bytes(1))},
                        data={"printer": "Fake_Printer"}).status_code for _ in range(3)]
        assert codes == [202, 202, 429]
        assert c.get("/api/jobs").status_code == 200        # reads still work
        r = c.post("/print", files={"file": ("a.pdf", pdf_bytes(1))},
                   data={"printer": "Fake_Printer"})
        assert r.status_code == 429 and "Too many requests" in r.text
