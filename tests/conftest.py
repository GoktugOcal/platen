from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.devices import FakePrinterBackend, FakeScannerBackend
from app.main import create_app


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        data_dir=tmp_path, backend="fake", poll_interval=0.01, discover_printers=False,
    )
    values.update(overrides)
    return Settings(**values)


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
def client(settings, printers, scanners):
    app = create_app(settings, printers, scanners)
    with TestClient(app) as c:
        yield c


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
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} stuck in {job['status']}")
