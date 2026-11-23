"""Business logic shared by the HTML UI and the JSON API.

Every operation on jobs takes the acting ``User``: regular users only see and
change their own jobs, admins see everything.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import UploadFile

from . import quotas
from .config import Settings
from .db import Database
from .delivery import PaperlessClient, PaperlessError
from .devices import (
    PrinterBackend,
    PrinterError,
    PrinterInfo,
    ScannerBackend,
    ScannerCaps,
    ScannerError,
    ScannerInfo,
)
from .devices.printers import name_from_uri, queue_name
from .jobs import JobRunner, deliver
from .models import (
    FINAL_STATUSES,
    Delivery,
    DeliveryKind,
    DeliveryStatus,
    Job,
    JobKind,
    JobStatus,
    ScanPage,
    User,
    new_id,
    utcnow,
)
from .models import SupplySample
from .pdf import (
    ALL_OFFICE_EXTENSIONS,
    EXTENSIONS,
    detect_office,
    detect_type,
    is_valid_page_range,
    make_thumbnail,
)
from .users import UserError, validate_email

log = logging.getLogger(__name__)

DUPLEX_OPTIONS = {
    "one-sided": "One-sided",
    "two-sided-long-edge": "Two-sided (long edge)",
    "two-sided-short-edge": "Two-sided (short edge)",
}
COLOR_OPTIONS = {"": "Printer default", "color": "Color", "monochrome": "Black & white"}
SCANNER_CACHE_SECONDS = 300
PAPERLESS_CACHE_SECONDS = 300


class ServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class PaperlessRequest:
    title: str = ""
    tags: list[int] = field(default_factory=list)
    correspondent: int | None = None
    document_type: int | None = None


@dataclass
class DeliveryRequest:
    email: str | None = None
    paperless: PaperlessRequest | None = None


class Services:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        printers: PrinterBackend,
        scanners: ScannerBackend,
        runner: JobRunner,
        discoverer=None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.printers = printers
        self.scanners = scanners
        self.runner = runner
        self._scanner_cache: tuple[float, list[ScannerInfo]] | None = None
        self._caps_cache: dict[str, ScannerCaps] = {}
        self._paperless_cache: dict[str, tuple[float, dict]] = {}
        self.device_messages: list[str] = []
        self.discoverer = discoverer
        self.device_origin: dict[str, str] = {}      # CUPS queue -> manual | discovered
        self.scanner_origin: dict[str, str] = {}     # airscan device name -> manual | discovered
        self._last_seen: dict[str, float] = {}
        self._scanners_seen: dict[str, tuple[str, str, float]] = {}  # label -> (uri, host, seen)
        self.last_discovery: dict | None = None

    # --- devices ------------------------------------------------------------

    async def list_printers(self, user: User | None = None) -> list[PrinterInfo]:
        """All printers, or only those ``user`` may print to."""
        try:
            printers = await asyncio.to_thread(self.printers.list_printers)
        except Exception as exc:  # noqa: BLE001 - CUPS down should not break pages
            log.warning("could not list printers: %s", exc)
            return []
        if user is not None:
            printers = [p for p in printers if user.may_use_printer(p.name)]
        return printers

    async def get_printer(self, name: str, user: User) -> PrinterInfo:
        if not user.may_use_printer(name):
            raise ServiceError("You are not allowed to use this printer", 403)
        for printer in await self.list_printers():
            if printer.name == name:
                return printer
        raise ServiceError(f"Unknown printer: {name}", 404)

    async def sync_devices(self) -> list[str]:
        """Set up printers and scanners from PLATEN_* settings plus network discovery.

        Manual entries always win; a discovered device on the same host is skipped.
        Discovered printers that stay unseen for ``discovery_forget_hours`` are removed.
        """
        from .container_setup import scanner_entries, write_airscan_conf
        from .discovery import DiscoveryResult, discover

        st = self.settings
        messages: list[str] = []
        found = DiscoveryResult()
        if self.discoverer is not None and (st.discover_printers or st.scanner_discovery):
            found = await asyncio.to_thread(discover, self.discoverer, st.discovery_timeout)

        # --- printers
        wanted: list[tuple[str, str, str, str]] = []   # (queue, uri, label, origin)
        manual_hosts: set[str] = set()
        for name, uri in st.printer_entries():
            label = name or name_from_uri(uri)
            wanted.append((queue_name(label), uri, label, "manual"))
            manual_hosts.add(urlparse(uri).hostname or uri)
        if st.discover_printers:
            for f in found.printers:
                if f.host in manual_hosts:
                    continue
                wanted.append((queue_name(f.label), f.uri, f.label, "discovered"))
        now = time.monotonic()
        for queue, uri, label, origin in wanted:
            try:
                await asyncio.to_thread(self.printers.ensure_queue, queue, uri, label)
            except PrinterError as exc:
                log.warning("%s", exc)
                messages.append(str(exc))
                continue
            self.device_origin[queue] = origin
            if origin == "discovered":
                self._last_seen[queue] = now
        forget = st.discovery_forget_hours * 3600
        for queue, seen in list(self._last_seen.items()):
            if now - seen > forget and self.device_origin.get(queue) == "discovered":
                try:
                    await asyncio.to_thread(self.printers.remove_queue, queue)
                    messages.append(f"Removed {queue}: not seen on the network for "
                                    f"{st.discovery_forget_hours} hours.")
                except PrinterError as exc:
                    log.warning("%s", exc)
                self._last_seen.pop(queue, None)
                self.device_origin.pop(queue, None)

        # --- scanners (sane-airscan reads its config on every scan, so rewriting is enough)
        entries = scanner_entries(st.scanners)
        manual_scan_hosts = {urlparse(url).hostname for _, url in entries}
        origins = {name: "manual" for name, _ in entries}
        if st.scanner_discovery:
            # Remember scanners across runs: one lost mDNS answer must not drop a scanner.
            for f in found.scanners:
                self._scanners_seen[f.label] = (f.uri, f.host, now)
            for label, (uri, host, seen) in sorted(self._scanners_seen.items()):
                if now - seen > forget:
                    del self._scanners_seen[label]
                    continue
                if host in manual_scan_hosts or label in origins:
                    continue
                entries.append((label, uri))
                origins[label] = "discovered"
        self.scanner_origin = origins
        if st.sane_config_dir.is_dir():
            try:
                if await asyncio.to_thread(write_airscan_conf, st.sane_config_dir, entries):
                    self._scanner_cache = None
                    self._caps_cache.clear()
            except OSError as exc:
                messages.append(f"Could not update the scanner config: {exc}")

        if not wanted and (st.discover_printers or not st.printer_entries()):
            messages.append("No printers found. Make sure the printer supports IPP Everywhere "
                            "/ AirPrint and is on the same network, or set PLATEN_PRINTERS.")
        self.last_discovery = {"printers": len(found.printers), "scanners": len(found.scanners),
                               "at": utcnow()}
        self.device_messages = messages
        return messages

    def scanner_source(self, device_id: str) -> str:
        """'manual' / 'discovered' for airscan devices (id: airscan:<n>:<name>)."""
        name = device_id.split(":", 2)[2] if device_id.count(":") >= 2 else device_id
        return self.scanner_origin.get(name, "")

    async def list_scanners(self, refresh: bool = False) -> list[ScannerInfo]:
        cached = self._scanner_cache
        if not refresh and cached and time.monotonic() - cached[0] < SCANNER_CACHE_SECONDS:
            return cached[1]
        try:
            scanners = await asyncio.to_thread(self.scanners.list_scanners)
        except ScannerError as exc:
            log.warning("could not list scanners: %s", exc)
            scanners = []
        self._scanner_cache = (time.monotonic(), scanners)
        if refresh:
            self._caps_cache.clear()
        return scanners

    async def get_scanner(self, device: str) -> ScannerInfo:
        for scanner in await self.list_scanners():
            if scanner.id == device:
                return scanner
        raise ServiceError(f"Unknown scanner: {device}", 404)

    async def scanner_caps(self, device: str) -> ScannerCaps:
        if device not in self._caps_cache:
            lock = self.runner.device_lock(device)
            async with lock:
                try:
                    caps = await asyncio.to_thread(self.scanners.capabilities, device)
                except ScannerError as exc:
                    raise ServiceError(f"Scanner not reachable: {exc}", 502) from exc
            self._caps_cache[device] = caps
        return self._caps_cache[device]

    # --- jobs ---------------------------------------------------------------

    def list_jobs(self, user: User, kind: str | None = None, status: str | None = None,
                  limit: int = 200) -> list[Job]:
        owner = None if user.is_admin else user.username
        return self.db.list_jobs(kind, status, limit, user=owner)

    def get_job(self, job_id: str, user: User) -> Job:
        job = self.db.get_job(job_id)
        # Hide other users' jobs completely (404, not 403).
        if job is None or not (user.is_admin or job.user == user.username):
            raise ServiceError("Job not found", 404)
        return job

    def job_dir(self, job_id: str) -> Path:
        return self.settings.jobs_dir / job_id

    def job_file(self, job_id: str, user: User) -> tuple[Job, Path]:
        job = self.get_job(job_id, user)
        if not job.file_path or job.files_purged:
            raise ServiceError("No file is stored for this job", 404)
        path = Path(job.file_path)
        if not path.is_file():
            raise ServiceError("The file is no longer available", 404)
        return job, path

    async def cancel_job(self, job_id: str, user: User) -> Job:
        job = self.get_job(job_id, user)
        if job.status in FINAL_STATUSES:
            raise ServiceError(f"Job is already {job.status}", 409)
        # The runner notices the status change; wait_print also cancels in CUPS.
        job = self.db.update_job(job_id, status=JobStatus.CANCELLED, finished_at=utcnow())
        if job.kind == JobKind.SCAN:
            self._purge(job)
        return job

    def _purge(self, job: Job) -> None:
        shutil.rmtree(self.job_dir(job.id), ignore_errors=True)
        self.db.update_job(job.id, files_purged=True)

    def cleanup(self) -> int:
        """Delete stored files of old jobs and abandon stale scan sessions."""
        now = utcnow()
        for job in self.db.stale_scan_sessions(now - timedelta(days=1)):
            self.db.update_job(job.id, status=JobStatus.CANCELLED, finished_at=now,
                               error="scan session abandoned")
        purged = 0
        for job in self.db.jobs_to_purge(now - timedelta(days=self.settings.retention_days)):
            self._purge(job)
            purged += 1
        return purged

    # --- printing -----------------------------------------------------------

    @property
    def accepted_upload_types(self) -> list[str]:
        types = [".pdf", ".jpg", ".jpeg", ".png", ".txt"]
        if self.settings.office_enabled:
            types += ALL_OFFICE_EXTENSIONS
        return types

    async def create_print_job(
        self,
        user: User,
        upload: UploadFile,
        printer: str,
        copies: int = 1,
        duplex: str = "one-sided",
        color_mode: str = "",
        page_range: str = "",
    ) -> Job:
        info = await self.get_printer(printer, user)
        try:
            quotas.check(self.db, user, 1, self.settings.quota_mode)
        except quotas.QuotaExceeded as exc:
            raise ServiceError(str(exc), 403) from exc
        if not 1 <= copies <= 99:
            raise ServiceError("Copies must be between 1 and 99")
        if duplex not in DUPLEX_OPTIONS:
            raise ServiceError("Invalid duplex option")
        if color_mode not in COLOR_OPTIONS:
            raise ServiceError("Invalid color mode")
        page_range = page_range.strip()
        if page_range and not is_valid_page_range(page_range):
            raise ServiceError("Page range must look like 1-3,5")
        if not upload or not upload.filename:
            raise ServiceError("Choose a file to print")

        job_id = new_id()
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            path, size = await self._store_upload(upload, job_dir)
        except ServiceError:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

        job = self.db.add_job(Job(
            id=job_id, kind=JobKind.PRINT, status=JobStatus.QUEUED, user=user.username,
            device=info.name, device_label=info.label,
            filename=Path(upload.filename).name[:200], file_path=str(path), file_size=size,
            copies=copies, duplex=duplex, color_mode=color_mode or None,
            page_range=page_range or None,
        ))
        self.runner.submit(job.id)
        return job

    async def _store_upload(self, upload: UploadFile, job_dir: Path) -> tuple[Path, int]:
        limit = self.settings.max_upload_mb * 1024 * 1024
        tmp = job_dir / "upload.part"
        size = 0
        head = b""
        with tmp.open("wb") as fh:
            while chunk := await upload.read(1024 * 1024):
                if not head:
                    head = chunk[:4096]
                size += len(chunk)
                if size > limit:
                    raise ServiceError(
                        f"File is larger than {self.settings.max_upload_mb} MB", 413
                    )
                fh.write(chunk)
        if size == 0:
            raise ServiceError("The file is empty")

        office_ext = detect_office(head, upload.filename or "")
        if office_ext:
            if not self.settings.office_enabled:
                raise ServiceError(
                    "Office documents need the Gotenberg add-on. Export to PDF and upload "
                    "that, or ask the admin to enable it.", 415)
            path = job_dir / f"input{office_ext}"
        else:
            file_type = detect_type(head)
            if file_type is None:
                allowed = ", ".join(self.accepted_upload_types)
                raise ServiceError(f"Unsupported file type. Allowed: {allowed}", 415)
            path = job_dir / f"input{EXTENSIONS[file_type]}"
        tmp.replace(path)
        return path, size

    # --- scanning -----------------------------------------------------------

    async def create_scan_session(self, user: User, device: str, dpi: int, mode: str) -> Job:
        scanner = await self.get_scanner(device)
        caps = await self.scanner_caps(device)
        if caps.resolutions and dpi not in caps.resolutions:
            raise ServiceError(f"Resolution {dpi} DPI is not supported by this scanner")
        if caps.modes and mode not in caps.modes:
            raise ServiceError(f"Mode {mode} is not supported by this scanner")
        job = self.db.add_job(Job(
            kind=JobKind.SCAN, status=JobStatus.OPEN, user=user.username, device=scanner.id,
            device_label=scanner.label, dpi=dpi, color_mode=mode, started_at=utcnow(),
        ))
        self.job_dir(job.id).mkdir(parents=True, exist_ok=True)
        return job

    def open_scan(self, job_id: str, user: User) -> Job:
        job = self.get_job(job_id, user)
        if job.kind != JobKind.SCAN:
            raise ServiceError("Not a scan job", 400)
        if job.status != JobStatus.OPEN:
            raise ServiceError(f"Scan session is {job.status}", 409)
        return job

    def open_sessions(self, user: User) -> list[Job]:
        return self.db.list_jobs(JobKind.SCAN, JobStatus.OPEN, 10, user=user.username)

    async def scan_page(self, job_id: str, user: User,
                        replace_page_id: str | None = None) -> ScanPage:
        job = self.open_scan(job_id, user)
        old = None
        if replace_page_id:
            old = self.db.get_page(job_id, replace_page_id)
            if old is None:
                raise ServiceError("Page not found", 404)
        lock = self.runner.device_lock(job.device)
        if lock.locked():
            raise ServiceError("The scanner is busy. Try again when the current scan finishes.",
                               409)
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        page_key = new_id()
        image = job_dir / f"page-{page_key}.jpg"
        thumb = job_dir / f"thumb-{page_key}.jpg"
        async with lock:
            try:
                await asyncio.to_thread(self.scanners.scan, job.device, job.dpi, job.color_mode,
                                        image)
            except ScannerError as exc:
                raise ServiceError(f"Scan failed: {exc}", 502) from exc
        await asyncio.to_thread(make_thumbnail, image, thumb)
        if old is not None:
            Path(old.path).unlink(missing_ok=True)
            Path(old.thumb_path).unlink(missing_ok=True)
            return self.db.update_page(old.id, path=str(image), thumb_path=str(thumb),
                                       created_at=utcnow())
        return self.db.add_page(job_id, str(image), str(thumb))

    def delete_page(self, job_id: str, page_id: str, user: User) -> None:
        self.open_scan(job_id, user)
        page = self.db.get_page(job_id, page_id)
        if page is None:
            raise ServiceError("Page not found", 404)
        Path(page.path).unlink(missing_ok=True)
        Path(page.thumb_path).unlink(missing_ok=True)
        self.db.delete_page(page.id)

    def page_thumb(self, job_id: str, page_id: str, user: User) -> Path:
        self.get_job(job_id, user)
        page = self.db.get_page(job_id, page_id)
        if page is None or not Path(page.thumb_path).is_file():
            raise ServiceError("Page not found", 404)
        return Path(page.thumb_path)

    async def finish_scan(self, job_id: str, user: User,
                          deliveries: DeliveryRequest | None = None) -> Job:
        self.open_scan(job_id, user)
        if not self.db.list_pages(job_id):
            raise ServiceError("Scan at least one page first", 409)
        rows = self._delivery_rows(job_id, user, deliveries or DeliveryRequest())
        for row in rows:
            self.db.add_delivery(row)
        job = self.db.update_job(job_id, status=JobStatus.QUEUED)
        self.runner.submit(job.id)
        return job

    # --- statistics and supplies --------------------------------------------

    def stats(self, viewer: User, range_key: str, user_filter: str | None = None):
        """Dashboard statistics. Regular users only ever see their own numbers."""
        from . import stats

        key, start, end = stats.resolve_range(range_key)
        owner = user_filter if viewer.is_admin else viewer.username
        jobs = self.db.jobs_between(start.astimezone(timezone.utc),
                                    end.astimezone(timezone.utc), owner or None)
        return stats.compute(jobs, key, start, end)

    def supply_history(self, days: int = 90) -> list[dict]:
        from . import stats

        return stats.supply_history(self.db.supply_samples_since(utcnow() - timedelta(days=days)))

    def latest_supplies(self) -> dict[str, list[SupplySample]]:
        out: dict[str, list[SupplySample]] = {}
        for sample in self.db.latest_supply_samples():
            out.setdefault(sample.printer, []).append(sample)
        return out

    async def sample_supplies(self) -> int:
        """Record the current toner/ink level of every printer."""
        rows = []
        for printer in await self.list_printers():
            try:
                markers = await asyncio.to_thread(self.printers.supply_levels, printer.name)
            except Exception as exc:  # noqa: BLE001 - a missing printer is not fatal
                log.info("supply levels for %s unavailable: %s", printer.name, exc)
                continue
            rows += [SupplySample(printer=printer.name, marker=m.name, color=m.color,
                                  level=m.level) for m in markers]
        await asyncio.to_thread(self.db.add_supply_samples, rows)
        return len(rows)

    def quota_status(self, user: User) -> quotas.QuotaStatus:
        return quotas.status(self.db, user)

    # --- deliveries ---------------------------------------------------------

    def _delivery_rows(self, job_id: str, user: User, req: DeliveryRequest) -> list[Delivery]:
        rows = []
        if req.email:
            if not self.settings.email_enabled:
                raise ServiceError("Email is not configured on this server")
            try:
                address = validate_email(req.email)
            except UserError as exc:
                raise ServiceError(exc.message) from exc
            rows.append(Delivery(job_id=job_id, kind=DeliveryKind.EMAIL, target=address))
        if req.paperless is not None:
            if not self.settings.paperless_enabled:
                raise ServiceError("Paperless is not configured on this server")
            if not (user.paperless_token or self.settings.paperless_token):
                raise ServiceError("Add your Paperless API token on the Account page first")
            p = req.paperless
            options = {"title": p.title.strip()[:128], "tags": p.tags,
                       "correspondent": p.correspondent, "document_type": p.document_type}
            rows.append(Delivery(job_id=job_id, kind=DeliveryKind.PAPERLESS,
                                 target=self.settings.paperless_url,
                                 options=json.dumps(options)))
        return rows

    def list_deliveries(self, job_id: str, user: User) -> list[Delivery]:
        self.get_job(job_id, user)
        return self.db.list_deliveries(job_id)

    async def retry_delivery(self, job_id: str, delivery_id: str, user: User) -> Delivery:
        job = self.get_job(job_id, user)
        delivery = self.db.get_delivery(delivery_id)
        if delivery is None or delivery.job_id != job_id:
            raise ServiceError("Delivery not found", 404)
        if delivery.status != DeliveryStatus.FAILED:
            raise ServiceError(f"Delivery is {delivery.status}, only failed ones can be retried",
                               409)
        if job.status != JobStatus.DONE:
            raise ServiceError("The scan has not finished", 409)
        delivery = self.db.update_delivery(delivery_id, status=DeliveryStatus.PENDING,
                                           error=None)
        self.runner.spawn(deliver(self.runner.context(job_id), delivery_id),
                          name=f"delivery-{delivery_id}")
        return delivery

    async def paperless_metadata(self, user: User) -> dict:
        """Tags, correspondents and document types for the scan form (cached)."""
        if not self.settings.paperless_enabled:
            raise ServiceError("Paperless is not configured on this server", 404)
        token = user.paperless_token or self.settings.paperless_token
        if not token:
            raise ServiceError("Add your Paperless API token on the Account page first", 400)
        cached = self._paperless_cache.get(token)
        if cached and time.monotonic() - cached[0] < PAPERLESS_CACHE_SECONDS:
            return cached[1]

        def fetch() -> dict:
            with PaperlessClient(self.settings.paperless_url, token,
                                 transport=self.runner.transports.get("paperless")) as client:
                return client.metadata()

        try:
            data = await asyncio.to_thread(fetch)
        except PaperlessError as exc:
            raise ServiceError(str(exc), 502) from exc
        self._paperless_cache[token] = (time.monotonic(), data)
        return data
