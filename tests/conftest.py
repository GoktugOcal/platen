from __future__ import annotations

import io
import os
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.devices import FakePrinterBackend, FakeScannerBackend
from app.main import create_app

ADMIN_PASSWORD = "admin-pass-123"


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        data_dir=tmp_path, backend="fake", poll_interval=0.01, discover_printers=False,
        password=ADMIN_PASSWORD, sane_config_dir=tmp_path / "no-sane",
        libreoffice="",  # tests opt in to Office conversion explicitly
        # Run the suite against Postgres with PLATEN_TEST_DATABASE_URL=postgresql+psycopg://...
        database_url=os.environ.get("PLATEN_TEST_DATABASE_URL", ""),
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def _clean_postgres():
    """With a shared Postgres test database, start every test from empty tables."""
    url = os.environ.get("PLATEN_TEST_DATABASE_URL")
    if url:
        from sqlalchemy import create_engine
        from sqlmodel import SQLModel

        import app.models  # noqa: F401 - register tables

        engine = create_engine(url)
        SQLModel.metadata.drop_all(engine)
        engine.dispose()
    yield


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def printers():
    return FakePrinterBackend()


@pytest.fixture
def scanners():
    return FakeScannerBackend()


@pytest.fixture
def transports():
    return {}


def login(client: TestClient, username: str = "admin", password: str = ADMIN_PASSWORD):
    r = client.post("/login", data={"username": username, "password": password},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


@pytest.fixture
def anon_client(settings, printers, scanners, transports):
    app = create_app(settings, printers, scanners, transports)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(anon_client):
    """Logged in as the bootstrap admin."""
    login(anon_client)
    return anon_client


def pdf_bytes(pages: int = 2) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def wait_for(client: TestClient, job_id: str, statuses=("done", "failed", "cancelled"),
             timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    job = {}
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job.get("status") in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} stuck in {job.get('status')}")


def wait_deliveries(client: TestClient, job_id: str, timeout: float = 10.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    items = []
    while time.monotonic() < deadline:
        items = client.get(f"/api/jobs/{job_id}/deliveries").json()
        if items and all(d["status"] in ("done", "failed") for d in items):
            return items
        time.sleep(0.02)
    raise AssertionError(f"deliveries stuck: {items}")


def scan_and_finish(client: TestClient, pages: int = 1, finish: dict | None = None) -> str:
    job_id = client.post("/api/scan", json={"scanner": "fake:scanner", "dpi": 150}).json()["id"]
    for _ in range(pages):
        assert client.post(f"/api/scan/{job_id}/pages").status_code == 201
    r = client.post(f"/api/scan/{job_id}/finish", json=finish or {})
    assert r.status_code == 202, r.text
    return job_id
