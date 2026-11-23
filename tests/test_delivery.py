from __future__ import annotations

import json
import smtplib

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import login, make_settings, pdf_bytes, scan_and_finish, wait_deliveries, wait_for


# --- fake SMTP ------------------------------------------------------------------

class FakeSMTP:
    sent: list = []
    fail_auth = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        if FakeSMTP.fail_auth:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.calls.append(("login", user))

    def send_message(self, msg):
        FakeSMTP.sent.append((self, msg))
        return {}


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent = []
    FakeSMTP.fail_auth = False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def email_app(tmp_path, printers, scanners, **overrides):
    settings = make_settings(
        tmp_path, smtp_host="smtp.gmail.com", smtp_user="me@gmail.com", smtp_password="app-pw",
        **overrides,
    )
    return create_app(settings, printers, scanners)


def test_scan_to_email(tmp_path, printers, scanners, smtp):
    with TestClient(email_app(tmp_path, printers, scanners)) as c:
        login(c)
        job_id = scan_and_finish(c, pages=2, finish={"email": "friend@example.com"})
        assert wait_for(c, job_id)["status"] == "done"
        [d] = wait_deliveries(c, job_id)
        assert d["status"] == "done" and d["result"] == "Sent to friend@example.com"
        server, msg = smtp.sent[0]
        assert (server.host, server.port) == ("smtp.gmail.com", 587)
        assert server.calls == ["starttls", ("login", "me@gmail.com")]
        assert msg["From"] == "me@gmail.com" and msg["To"] == "friend@example.com"
        [attachment] = list(msg.iter_attachments())
        assert attachment.get_content_type() == "application/pdf"
        assert attachment.get_content().startswith(b"%PDF-")


def test_email_failure_keeps_job_done_and_can_retry(tmp_path, printers, scanners, smtp):
    with TestClient(email_app(tmp_path, printers, scanners)) as c:
        login(c)
        smtp.fail_auth = True
        job_id = scan_and_finish(c, finish={"email": "friend@example.com"})
        assert wait_for(c, job_id)["status"] == "done"
        [d] = wait_deliveries(c, job_id)
        assert d["status"] == "failed" and "SMTP login failed" in d["error"]
        assert c.get(f"/api/jobs/{job_id}/file").status_code == 200  # PDF still there
        assert "Retry" in c.get(f"/jobs/{job_id}").text

        smtp.fail_auth = False
        r = c.post(f"/api/jobs/{job_id}/deliveries/{d['id']}/retry")
        assert r.status_code == 202
        [d] = wait_deliveries(c, job_id)
        assert d["status"] == "done" and d["attempts"] == 2
        assert c.post(f"/api/jobs/{job_id}/deliveries/{d['id']}/retry").status_code == 409


def test_email_size_limit(tmp_path, printers, scanners, smtp):
    with TestClient(email_app(tmp_path, printers, scanners, email_max_mb=0.001)) as c:
        login(c)
        job_id = scan_and_finish(c, finish={"email": "friend@example.com"})
        [d] = wait_deliveries(c, job_id)
        assert d["status"] == "failed" and "email limit" in d["error"]
        assert smtp.sent == []


def test_delivery_validation(client, tmp_path, printers, scanners):
    # Email/Paperless not configured on the default test app.
    job_id = client.post("/api/scan", json={"scanner": "fake:scanner"}).json()["id"]
    client.post(f"/api/scan/{job_id}/pages")
    r = client.post(f"/api/scan/{job_id}/finish", json={"email": "a@example.com"})
    assert r.status_code == 400 and "not configured" in r.json()["detail"]
    r = client.post(f"/api/scan/{job_id}/finish", json={"paperless": {}})
    assert r.status_code == 400
    with TestClient(email_app(tmp_path / "x", printers, scanners)) as c:
        login(c)
        job_id = c.post("/api/scan", json={"scanner": "fake:scanner"}).json()["id"]
        c.post(f"/api/scan/{job_id}/pages")
        r = c.post(f"/api/scan/{job_id}/finish", json={"email": "not-an-address"})
        assert r.status_code == 400


def test_scan_finish_form_with_email(tmp_path, printers, scanners, smtp):
    with TestClient(email_app(tmp_path, printers, scanners)) as c:
        login(c)
        r = c.post("/scan", data={"device": "fake:scanner", "dpi": "150", "mode": "Gray"},
                   follow_redirects=False)
        url = r.headers["location"]
        assert "Send to" in c.get(url).text
        c.post(f"{url}/pages")
        r = c.post(f"{url}/finish", data={"deliver_email": "1", "email": "x@example.com"},
                   follow_redirects=False)
        assert r.status_code == 303
        job_id = url.rsplit("/", 1)[1]
        [d] = wait_deliveries(c, job_id)
        assert d["status"] == "done"


# --- fake Paperless ---------------------------------------------------------------

class FakePaperless:
    def __init__(self, outcome="SUCCESS", pending_polls=1):
        self.outcome = outcome
        self.pending_polls = pending_polls
        self.uploads = []
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] in ("Token user-token", "Token global-token")
        path = request.url.path
        if path == "/api/documents/post_document/":
            self.uploads.append((request.headers["authorization"], request.content))
            return httpx.Response(200, json="task-123")
        if path == "/api/tasks/":
            assert request.url.params["task_id"] == "task-123"
            self.polls += 1
            if self.polls <= self.pending_polls:
                return httpx.Response(200, json=[{"status": "STARTED"}])
            if self.outcome == "SUCCESS":
                return httpx.Response(200, json=[{"status": "SUCCESS", "related_document": "42"}])
            if self.outcome == "PENDING":
                return httpx.Response(200, json=[{"status": "PENDING"}])
            return httpx.Response(200, json=[{"status": "FAILURE",
                                              "result": "scan.pdf: Not consuming: It is a duplicate."}])
        if path == "/api/tags/":
            return httpx.Response(200, json={"next": None, "results": [
                {"id": 2, "name": "taxes"}, {"id": 1, "name": "Bills"}]})
        if path == "/api/correspondents/":
            return httpx.Response(200, json={"next": None, "results": [{"id": 7, "name": "Bank"}]})
        if path == "/api/document_types/":
            return httpx.Response(200, json={"next": None, "results": []})
        return httpx.Response(404)


def paperless_app(tmp_path, printers, scanners, fake, **overrides):
    overrides.setdefault("paperless_wait", 5)
    settings = make_settings(tmp_path, paperless_url="http://paperless.local:8000", **overrides)
    return create_app(settings, printers, scanners,
                      transports={"paperless": httpx.MockTransport(fake.handler)})


def _set_token(c, token="user-token"):
    assert c.post("/account/profile", data={"email": "", "paperless_token": token}
                  ).status_code == 200


def test_scan_to_paperless(tmp_path, printers, scanners):
    fake = FakePaperless()
    with TestClient(paperless_app(tmp_path, printers, scanners, fake)) as c:
        login(c)
        # Without a token (and no global token) the delivery is refused up front.
        job_id = c.post("/api/scan", json={"scanner": "fake:scanner"}).json()["id"]
        c.post(f"/api/scan/{job_id}/pages")
        assert c.post(f"/api/scan/{job_id}/finish", json={"paperless": {}}).status_code == 400

        _set_token(c)
        meta = c.get("/api/paperless/metadata").json()
        assert [t["name"] for t in meta["tags"]] == ["Bills", "taxes"]
        assert meta["correspondents"] == [{"id": 7, "name": "Bank"}]

        r = c.post(f"/api/scan/{job_id}/finish", json={"paperless": {
            "title": "Bank letter", "tags": [1, 2], "correspondent": 7}})
        assert r.status_code == 202
        [d] = wait_deliveries(c, job_id)
        assert d["status"] == "done", d
        assert d["result"] == "Added to Paperless as document #42"
        assert d["result_url"] == "http://paperless.local:8000/documents/42/details"
        auth, body = fake.uploads[0]
        assert auth == "Token user-token"
        assert b'name="title"' in body and b"Bank letter" in body
        assert body.count(b'name="tags"') == 2 and b'name="correspondent"' in body
        assert json.loads(d["options"])["tags"] == [1, 2]


def test_paperless_duplicate_fails_delivery_only(tmp_path, printers, scanners):
    fake = FakePaperless(outcome="FAILURE")
    with TestClient(paperless_app(tmp_path, printers, scanners, fake,
                                  paperless_token="global-token")) as c:
        login(c)
        job_id = scan_and_finish(c, finish={"paperless": {}})
        assert wait_for(c, job_id)["status"] == "done"
        [d] = wait_deliveries(c, job_id)
        assert d["status"] == "failed" and "duplicate" in d["error"]
        assert fake.uploads[0][0] == "Token global-token"


def test_paperless_still_processing(tmp_path, printers, scanners):
    fake = FakePaperless(outcome="PENDING")
    with TestClient(paperless_app(tmp_path, printers, scanners, fake, paperless_wait=0,
                                  paperless_token="global-token")) as c:
        login(c)
        job_id = scan_and_finish(c, finish={"paperless": {}})
        [d] = wait_deliveries(c, job_id)
        assert d["status"] == "done" and "still processing" in d["result"]


def test_paperless_fields_fragment(tmp_path, printers, scanners):
    fake = FakePaperless()
    with TestClient(paperless_app(tmp_path, printers, scanners, fake)) as c:
        login(c)
        _set_token(c)
        job_id = c.post("/api/scan", json={"scanner": "fake:scanner"}).json()["id"]
        html = c.get(f"/scan/{job_id}/paperless-fields").text
        assert "Bank" in html and "taxes" in html


# --- Office documents via Gotenberg -------------------------------------------------

DOCX = b"PK\x03\x04" + b"\x00" * 64


def test_office_rejected_without_gotenberg(client):
    r = client.post("/api/print", files={"file": ("letter.docx", DOCX)},
                    data={"printer": "Fake_Printer"})
    assert r.status_code == 415 and "Gotenberg" in r.json()["detail"]


def test_office_converted_with_gotenberg(tmp_path, printers, scanners):
    seen = []

    def gotenberg(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert b'filename="input.docx"' in request.content
        return httpx.Response(200, content=pdf_bytes(3),
                              headers={"content-type": "application/pdf"})

    settings = make_settings(tmp_path, gotenberg_url="http://127.0.0.1:3100")
    app = create_app(settings, printers, scanners,
                     transports={"gotenberg": httpx.MockTransport(gotenberg)})
    with TestClient(app) as c:
        login(c)
        assert ".docx" in c.get("/print").text
        r = c.post("/api/print", files={"file": ("letter.docx", DOCX)},
                   data={"printer": "Fake_Printer"})
        assert r.status_code == 202, r.text
        job = wait_for(c, r.json()["id"])
        assert job["status"] == "done" and job["pages"] == 3
        assert job["filename"] == "letter.docx"
        assert seen == ["/forms/libreoffice/convert"]
        submitted = printers.jobs[job["cups_job_id"]]
        assert submitted["path"].endswith("converted.pdf")


def test_office_conversion_failure(tmp_path, printers, scanners):
    settings = make_settings(tmp_path, gotenberg_url="http://127.0.0.1:3100")
    app = create_app(settings, printers, scanners, transports={
        "gotenberg": httpx.MockTransport(lambda r: httpx.Response(503, text="busy"))})
    with TestClient(app) as c:
        login(c)
        r = c.post("/api/print", files={"file": ("sheet.xlsx", DOCX)},
                   data={"printer": "Fake_Printer"})
        job = wait_for(c, r.json()["id"])
        assert job["status"] == "failed" and "Conversion failed (503)" in job["error"]
