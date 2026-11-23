"""Application factory. Run with: uvicorn app.main:create_app --factory"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import AuthMiddleware, Authenticator, load_secret_key
from .config import Settings, get_settings
from .db import Database
from .devices import (
    CupsPrinterBackend,
    FakePrinterBackend,
    FakeScannerBackend,
    PrinterBackend,
    SaneScannerBackend,
    ScannerBackend,
)
from .jobs import PIPELINES, RESUME_PRINT, JobRunner
from .routes import api, ui
from .services import ServiceError, Services

log = logging.getLogger("platen")
APP_DIR = Path(__file__).parent
CLEANUP_INTERVAL = 3600


def _backends(settings: Settings) -> tuple[PrinterBackend, ScannerBackend]:
    if settings.backend == "fake":
        return FakePrinterBackend(), FakeScannerBackend()
    return CupsPrinterBackend(), SaneScannerBackend(settings.scan_timeout)


def _cups_socket_ok() -> bool:
    server = os.environ.get("CUPS_SERVER", "/run/cups/cups.sock")
    return not server.startswith("/") or Path(server).exists()


async def _cleanup_loop(services: Services) -> None:
    while True:
        try:
            purged = await asyncio.to_thread(services.cleanup)
            if purged:
                log.info("retention cleanup removed files of %d jobs", purged)
        except Exception:  # noqa: BLE001
            log.exception("cleanup failed")
        await asyncio.sleep(CLEANUP_INTERVAL)


async def _initial_sync(services: Services) -> None:
    try:
        messages = await services.sync_printers()
        for msg in messages:
            log.warning("printer setup: %s", msg)
    except Exception:  # noqa: BLE001
        log.exception("printer setup failed")


def create_app(
    settings: Settings | None = None,
    printers: PrinterBackend | None = None,
    scanners: ScannerBackend | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)

    default_printers, default_scanners = _backends(settings)
    printers = printers or default_printers
    scanners = scanners or default_scanners
    authenticator = Authenticator(
        settings.password, load_secret_key(settings.secret_key, settings.data_dir)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.db_path)
        runner = JobRunner(db, settings, printers, scanners, PIPELINES)
        services = Services(settings, db, printers, scanners, runner)
        app.state.db = db
        app.state.runner = runner
        app.state.services = services
        runner.start()
        runner.recover(RESUME_PRINT)
        background = [
            asyncio.create_task(_initial_sync(services)),
            asyncio.create_task(_cleanup_loop(services)),
        ]
        if not settings.password:
            log.warning("PLATEN_PASSWORD is not set: anyone who can reach this port can use it")
        try:
            yield
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await runner.stop()

    app = FastAPI(title="Platen", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.auth = authenticator
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    app.include_router(api.router)
    app.include_router(ui.router)

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.message}, status_code=exc.status_code)
        return ui.render_error(request, exc)

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request):
        checks = {
            "db": _safe(request.app.state.db.ping),
            "cups": settings.backend == "fake" or _cups_socket_ok(),
            "runner": request.app.state.runner.alive,
        }
        ok = all(checks.values())
        return JSONResponse({"ok": ok, **checks}, status_code=200 if ok else 503)

    return app


def _safe(fn) -> bool:
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return False
