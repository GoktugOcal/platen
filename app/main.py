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
from .auth import AuthMiddleware, Sessions, load_secret_key
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
from .routes import account, admin, api, ui
from .security import RateLimitMiddleware, SecurityMiddleware
from .services import ServiceError, Services
from .users import LoginThrottle, UserError, UserService

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


async def _supply_loop(services: Services, minutes: int) -> None:
    await asyncio.sleep(20)  # let printer queues come up first
    while True:
        try:
            count = await services.sample_supplies()
            log.info("recorded %d supply level samples", count)
        except Exception:  # noqa: BLE001
            log.exception("supply sampling failed")
        await asyncio.sleep(minutes * 60)


STARTUP_RESCAN_SECONDS = 60


async def _device_loop(services: Services, minutes: int) -> None:
    """Set up devices at start, again shortly after (the first mDNS answers right after
    container start are sometimes lost), then re-run discovery periodically."""
    first = True
    while True:
        try:
            messages = await services.sync_devices()
            for msg in messages:
                log.warning("device setup: %s", msg)
            found = services.last_discovery or {}
            log.info("device sync: %d queues, discovery saw %s printer(s) and %s scanner(s)",
                     len(services.device_origin), found.get("printers", 0),
                     found.get("scanners", 0))
        except Exception:  # noqa: BLE001
            log.exception("device setup failed")
        if first:
            first = False
            await asyncio.sleep(STARTUP_RESCAN_SECONDS)
            continue
        if minutes <= 0:
            return
        await asyncio.sleep(minutes * 60)


def create_app(
    settings: Settings | None = None,
    printers: PrinterBackend | None = None,
    scanners: ScannerBackend | None = None,
    transports: dict | None = None,
    discoverer=None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)

    default_printers, default_scanners = _backends(settings)
    printers = printers or default_printers
    scanners = scanners or default_scanners
    if discoverer is None:
        from .discovery import FakeDiscoverer, ZeroconfDiscoverer

        discoverer = FakeDiscoverer() if settings.backend == "fake" else ZeroconfDiscoverer()
    sessions = Sessions(load_secret_key(settings.secret_key, settings.data_dir))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.db_url)
        users = UserService(db)
        if users.bootstrap(settings.password):
            log.info("created admin user 'admin' from PLATEN_PASSWORD")
        runner = JobRunner(db, settings, printers, scanners, PIPELINES, transports)
        services = Services(settings, db, printers, scanners, runner, discoverer)
        app.state.db = db
        app.state.users = users
        app.state.runner = runner
        app.state.services = services
        runner.start()
        runner.recover(RESUME_PRINT)
        background = [
            asyncio.create_task(_device_loop(services, settings.discovery_interval)),
            asyncio.create_task(_cleanup_loop(services)),
        ]
        if settings.supply_sample_minutes > 0 and settings.backend != "fake":
            background.append(asyncio.create_task(
                _supply_loop(services, settings.supply_sample_minutes)))
        if users.needs_setup:
            log.warning("no users yet: open the web UI and create the first admin at /setup")
        try:
            yield
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await runner.stop()

    app = FastAPI(title="Platen", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.sessions = sessions
    app.state.login_throttle = LoginThrottle()
    # Starlette runs the last-added middleware first: Security -> Auth -> RateLimit -> app.
    app.add_middleware(RateLimitMiddleware,
                       requests_per_minute=settings.rate_limit_per_minute,
                       heavy_per_minute=settings.heavy_rate_limit_per_minute)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(SecurityMiddleware,
                       max_body_bytes=(settings.max_upload_mb + 1) * 1024 * 1024,
                       trusted_origins=settings.trusted_origin_set)
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    app.include_router(api.router)
    app.include_router(account.router)
    app.include_router(admin.router)
    app.include_router(ui.router)

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.message}, status_code=exc.status_code)
        return ui.render_error(request, exc.message, exc.status_code)

    @app.exception_handler(UserError)
    async def _user_error(request: Request, exc: UserError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.message}, status_code=exc.status_code)
        return ui.render_error(request, exc.message, exc.status_code)

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
