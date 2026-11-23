"""Business logic shared by the HTML UI and the JSON API."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import timedelta
from pathlib import Path

from fastapi import UploadFile

from .config import Settings
from .db import Database
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
from .jobs import JobRunner
from .models import FINAL_STATUSES, Job, JobKind, JobStatus, ScanPage, new_id, utcnow
from .pdf import EXTENSIONS, detect_type, is_valid_page_range, make_thumbnail

log = logging.getLogger(__name__)

DUPLEX_OPTIONS = {
    "one-sided": "One-sided",
    "two-sided-long-edge": "Two-sided (long edge)",
    "two-sided-short-edge": "Two-sided (short edge)",
}
COLOR_OPTIONS = {"": "Printer default", "color": "Color", "monochrome": "Black & white"}
SCANNER_CACHE_SECONDS = 300


class ServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class Services:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        printers: PrinterBackend,
        scanners: ScannerBackend,
        runner: JobRunner,
    ) -> None:
        self.settings = settings
        self.db = db
        self.printers = printers
        self.scanners = scanners
        self.runner = runner
        self._scanner_cache: tuple[float, list[ScannerInfo]] | None = None
        self._caps_cache: dict[str, ScannerCaps] = {}
        self.device_messages: list[str] = []

    # --- devices ------------------------------------------------------------

    async def list_printers(self) -> list[PrinterInfo]:
        try:
            return await asyncio.to_thread(self.printers.list_printers)
        except Exception as exc:  # noqa: BLE001 - CUPS down should not break pages
            log.warning("could not list printers: %s", exc)
            return []

    async def get_printer(self, name: str) -> PrinterInfo:
        for printer in await self.list_printers():
            if printer.name == name:
                return printer
        raise ServiceError(f"Unknown printer: {name}", 404)

    async def sync_printers(self) -> list[str]:
        """Create CUPS queues for configured (or discovered) printers."""
        messages: list[str] = []
        entries = self.settings.printer_entries()
        if not entries and self.settings.discover_printers:
            try:
                discovered = await asyncio.to_thread(self.printers.discover)
            except PrinterError as exc:
                discovered = []
                messages.append(str(exc))
            entries = [(label, uri) for label, uri in discovered]
            if not discovered:
                messages.append(
                    "No IPP Everywhere printers were found via mDNS. If discovery is blocked "
                    "(e.g. AppArmor denies D-Bus to containers), set PLATEN_PRINTERS."
                )
        for name, uri in entries:
            label = name or name_from_uri(uri)
            try:
                await asyncio.to_thread(self.printers.ensure_queue, queue_name(label), uri, label)
            except PrinterError as exc:
                log.warning("%s", exc)
                messages.append(str(exc))
        self.device_messages = messages
        return messages

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

    def get_job(self, job_id: str) -> Job:
        job = self.db.get_job(job_id)
        if job is None:
            raise ServiceError("Job not found", 404)
        return job

    def job_dir(self, job_id: str) -> Path:
        return self.settings.jobs_dir / job_id

    def job_file(self, job_id: str) -> tuple[Job, Path]:
        job = self.get_job(job_id)
        if not job.file_path or job.files_purged:
            raise ServiceError("No file is stored for this job", 404)
        path = Path(job.file_path)
        if not path.is_file():
            raise ServiceError("The file is no longer available", 404)
        return job, path

    async def cancel_job(self, job_id: str) -> Job:
        job = self.get_job(job_id)
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

    async def create_print_job(
        self,
        upload: UploadFile,
        printer: str,
        copies: int = 1,
        duplex: str = "one-sided",
        color_mode: str = "",
        page_range: str = "",
    ) -> Job:
        info = await self.get_printer(printer)
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
            id=job_id, kind=JobKind.PRINT, status=JobStatus.QUEUED,
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
        file_type = detect_type(head)
        if file_type is None:
            raise ServiceError("Unsupported file type. Upload a PDF, JPEG, PNG or plain text file.",
                               415)
        path = job_dir / f"input{EXTENSIONS[file_type]}"
        tmp.replace(path)
        return path, size

    # --- scanning -----------------------------------------------------------

    async def create_scan_session(self, device: str, dpi: int, mode: str) -> Job:
        scanner = await self.get_scanner(device)
        caps = await self.scanner_caps(device)
        if caps.resolutions and dpi not in caps.resolutions:
            raise ServiceError(f"Resolution {dpi} DPI is not supported by this scanner")
        if caps.modes and mode not in caps.modes:
            raise ServiceError(f"Mode {mode} is not supported by this scanner")
        job = self.db.add_job(Job(
            kind=JobKind.SCAN, status=JobStatus.OPEN, device=scanner.id,
            device_label=scanner.label, dpi=dpi, color_mode=mode, started_at=utcnow(),
        ))
        self.job_dir(job.id).mkdir(parents=True, exist_ok=True)
        return job

    def _open_scan(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if job.kind != JobKind.SCAN:
            raise ServiceError("Not a scan job", 400)
        if job.status != JobStatus.OPEN:
            raise ServiceError(f"Scan session is {job.status}", 409)
        return job

    async def scan_page(self, job_id: str, replace_page_id: str | None = None) -> ScanPage:
        job = self._open_scan(job_id)
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

    def delete_page(self, job_id: str, page_id: str) -> None:
        self._open_scan(job_id)
        page = self.db.get_page(job_id, page_id)
        if page is None:
            raise ServiceError("Page not found", 404)
        Path(page.path).unlink(missing_ok=True)
        Path(page.thumb_path).unlink(missing_ok=True)
        self.db.delete_page(page.id)

    def page_thumb(self, job_id: str, page_id: str) -> Path:
        page = self.db.get_page(job_id, page_id)
        if page is None or not Path(page.thumb_path).is_file():
            raise ServiceError("Page not found", 404)
        return Path(page.thumb_path)

    async def finish_scan(self, job_id: str) -> Job:
        self._open_scan(job_id)
        if not self.db.list_pages(job_id):
            raise ServiceError("Scan at least one page first", 409)
        job = self.db.update_job(job_id, status=JobStatus.QUEUED)
        self.runner.submit(job.id)
        return job
