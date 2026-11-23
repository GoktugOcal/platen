from __future__ import annotations

from fastapi.testclient import TestClient
from pypdf import PdfReader
import io

from app.main import create_app

from .conftest import login, make_settings, pdf_bytes, wait_for


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_print_flow(client, printers):
    r = client.post(
        "/api/print",
        files={"file": ("report.pdf", pdf_bytes(5), "application/pdf")},
        data={"printer": "Fake_Printer", "copies": "2", "duplex": "two-sided-long-edge",
              "page_range": "1-3"},
    )
    assert r.status_code == 202, r.text
    job = wait_for(client, r.json()["id"])
    assert job["status"] == "done"
    assert job["pages"] == 3          # counted by us, page range applied
    assert job["copies"] == 2
    assert job["filename"] == "report.pdf"
    submitted = printers.jobs[job["cups_job_id"]]
    assert submitted["options"] == {
        "copies": "2", "sides": "two-sided-long-edge", "page-ranges": "1-3",
    }


def test_print_failure_is_recorded(client, printers):
    printers.fail_next = True
    r = client.post("/api/print", files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)},
                    data={"printer": "Fake_Printer"})
    job = wait_for(client, r.json()["id"])
    assert job["status"] == "failed"
    assert "aborted" in job["error"]
    assert job["pages"] == 1


def test_print_validation(client):
    def post(**kw):
        files = {"file": kw.pop("file", ("a.pdf", pdf_bytes(1)))}
        data = {"printer": "Fake_Printer", **kw}
        return client.post("/api/print", files=files, data=data)

    assert post(file=("a.docx", b"PK\x03\x04" + b"\x00" * 10)).status_code == 415
    assert post(file=("a.pdf", b"")).status_code == 400
    assert post(printer="Nope").status_code == 404
    assert post(copies="0").status_code == 400
    assert post(duplex="sideways").status_code == 400
    assert post(page_range="3-1").status_code == 400
    assert client.get("/api/jobs").json() == []


def test_upload_size_limit(tmp_path, printers, scanners):
    app = create_app(make_settings(tmp_path, max_upload_mb=1), printers, scanners)
    with TestClient(app) as c:
        login(c)
        big = b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 10)
        r = c.post("/api/print", files={"file": ("big.pdf", big)}, data={"printer": "Fake_Printer"})
        assert r.status_code == 413


def test_cancel_print(tmp_path, scanners):
    from app.devices import FakePrinterBackend

    slow = FakePrinterBackend(steps_to_complete=10_000)
    app = create_app(make_settings(tmp_path), slow, scanners)
    with TestClient(app) as c:
        login(c)
        r = c.post("/api/print", files={"file": ("a.pdf", pdf_bytes(1))},
                   data={"printer": "Fake_Printer"})
        job_id = r.json()["id"]
        wait_for(c, job_id, statuses=("printing",))
        assert c.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        job = wait_for(c, job_id)
        assert job["status"] == "cancelled"
        assert slow.jobs[job["cups_job_id"]]["state"] == "cancelled"
        assert c.post(f"/api/jobs/{job_id}/cancel").status_code == 409


def test_scan_flow(client, scanners):
    assert client.get("/api/scanners").json() == [{"id": "fake:scanner", "label": "Fake Scanner"}]
    caps = client.get("/api/scanners/capabilities", params={"device": "fake:scanner"}).json()
    assert caps["modes"] == ["Color", "Gray"]

    job = client.post("/api/scan", json={"scanner": "fake:scanner", "dpi": 150}).json()
    assert job["status"] == "open" and job["color_mode"] == "Gray"
    job_id = job["id"]

    assert client.post(f"/api/scan/{job_id}/finish").status_code == 409  # no pages yet
    p1 = client.post(f"/api/scan/{job_id}/pages").json()
    client.post(f"/api/scan/{job_id}/pages")
    p3 = client.post(f"/api/scan/{job_id}/pages").json()
    assert client.delete(f"/api/scan/{job_id}/pages/{p3['id']}").status_code == 204
    assert client.get(p1["thumbnail_url"]).headers["content-type"] == "image/jpeg"

    scanners.fail_next = True
    r = client.post(f"/api/scan/{job_id}/pages")
    assert r.status_code == 502 and "jammed" in r.json()["detail"]

    assert client.post(f"/api/scan/{job_id}/finish").status_code == 202
    done = wait_for(client, job_id)
    assert done["status"] == "done" and done["pages"] == 2 and done["has_file"]

    pdf = client.get(f"/api/jobs/{job_id}/file")
    assert pdf.status_code == 200
    assert len(PdfReader(io.BytesIO(pdf.content)).pages) == 2
    # Session is closed now.
    assert client.post(f"/api/scan/{job_id}/pages").status_code == 409


def test_scan_rejects_unsupported_settings(client):
    r = client.post("/api/scan", json={"scanner": "fake:scanner", "dpi": 4800})
    assert r.status_code == 400
    r = client.post("/api/scan", json={"scanner": "nope"})
    assert r.status_code == 404


def test_retention_cleanup(client, settings):
    r = client.post("/api/print", files={"file": ("a.pdf", pdf_bytes(1))},
                    data={"printer": "Fake_Printer"})
    job_id = wait_for(client, r.json()["id"])["id"]
    services = client.app.state.services
    db = client.app.state.db
    from datetime import timedelta

    from app.models import utcnow

    db.update_job(job_id, finished_at=utcnow() - timedelta(days=settings.retention_days + 1))
    assert services.cleanup() == 1
    assert not (settings.jobs_dir / job_id).exists()
    assert client.get(f"/api/jobs/{job_id}").json()["has_file"] is False


def test_ui_pages_render(client):
    for path in ("/print", "/scan", "/jobs", "/devices", "/jobs?kind=print&status=done"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Platen" in r.text

    r = client.post("/print", files={"file": ("a.pdf", pdf_bytes(1))},
                    data={"printer": "Fake_Printer", "copies": "1"}, follow_redirects=False)
    assert r.status_code == 303
    job_page = client.get(r.headers["location"])
    assert job_page.status_code == 200 and "Print job" in job_page.text

    r = client.post("/scan", data={"device": "fake:scanner", "dpi": "200", "mode": "Gray"},
                    follow_redirects=False)
    session_url = r.headers["location"]
    page = client.post(f"{session_url}/pages", headers={"HX-Request": "true"})
    assert page.status_code == 200 and "Page 1" in page.text
    r = client.post(f"{session_url}/finish", follow_redirects=False)
    assert r.status_code == 303

    bad = client.post("/print", files={"file": ("a.zip", b"PK\x03\x04\x00\x00")},
                      data={"printer": "Fake_Printer"})
    assert bad.status_code == 415 and "Unsupported file type" in bad.text

    for path in ("/account", "/admin/users"):
        assert client.get(path).status_code == 200, path
