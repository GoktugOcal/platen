"""Pipeline steps. Each step does one thing and records its result on the job.

Print:  count_print_pages -> submit_print -> wait_print
Scan:   build_scan_pdf   (pages are captured interactively before the job is queued)

Future steps (OCR, email, Paperless upload) are added to these lists.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ..devices import PrinterError
from ..models import JobStatus
from ..pdf import build_pdf, count_pages, detect_type, page_range_count
from .runner import JobCancelled, JobContext


async def count_print_pages(ctx: JobContext) -> None:
    """Count pages ourselves; CUPS counts are unreliable for driverless printers."""
    job = ctx.job()
    path = Path(job.file_path)
    with path.open("rb") as fh:
        file_type = detect_type(fh.read(4096))
    total = await asyncio.to_thread(count_pages, path, file_type or "")
    pages = page_range_count(job.page_range, total) if total else None
    ctx.update(pages=pages)


async def submit_print(ctx: JobContext) -> None:
    job = ctx.job()
    options = {"copies": str(job.copies)}
    if job.duplex:
        options["sides"] = job.duplex
    if job.color_mode:
        options["print-color-mode"] = job.color_mode
    if job.page_range:
        options["page-ranges"] = job.page_range.replace(" ", "")
    title = f"platen-{job.id} {job.filename or ''}".strip()
    cups_id = await asyncio.to_thread(
        ctx.printers.submit, job.device, Path(job.file_path), title, options
    )
    if ctx.job().status == JobStatus.CANCELLED:  # cancelled while we were submitting
        ctx.update(cups_job_id=cups_id)
        await asyncio.to_thread(ctx.printers.cancel, cups_id)
        raise JobCancelled()
    ctx.update(cups_job_id=cups_id, status=JobStatus.PRINTING)


async def wait_print(ctx: JobContext) -> None:
    """Poll CUPS until the job finishes. Honours cancellation from the UI."""
    while True:
        job = ctx.job()
        if job.status == JobStatus.CANCELLED:
            await asyncio.to_thread(ctx.printers.cancel, job.cups_job_id)
            raise JobCancelled()
        state = await asyncio.to_thread(ctx.printers.job_state, job.cups_job_id)
        if state.state == "completed":
            if job.pages is None and state.impressions:
                ctx.update(pages=state.impressions // max(job.copies, 1))
            return
        if state.state == "cancelled":
            raise JobCancelled()
        if state.state == "aborted":
            reasons = ", ".join(state.reasons) or "unknown reason"
            raise PrinterError(f"CUPS aborted the job ({reasons})")
        await asyncio.sleep(ctx.settings.poll_interval)


async def build_scan_pdf(ctx: JobContext) -> None:
    job = ctx.job()
    pages = ctx.db.list_pages(job.id)
    if not pages:
        raise ValueError("no pages were scanned")
    out = ctx.job_dir / f"scan-{job.created_at:%Y%m%d-%H%M%S}.pdf"
    await asyncio.to_thread(build_pdf, [Path(p.path) for p in pages], out, job.dpi)
    ctx.update(
        file_path=str(out), filename=out.name, file_size=out.stat().st_size, pages=len(pages)
    )


PIPELINES = {
    "print": [count_print_pages, submit_print, wait_print],
    "scan": [build_scan_pdf],
}

RESUME_PRINT = [wait_print]
