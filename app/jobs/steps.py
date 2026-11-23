"""Pipeline steps. Each step does one thing and records its result on the job.

Print:  convert_document -> count_print_pages -> submit_print -> wait_print
Scan:   build_scan_pdf -> run_deliveries
        (pages are captured interactively before the job is queued)

Future steps (OCR, ...) are added to these lists.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from ..convert import convert_office
from ..delivery import EmailError, PaperlessClient, PaperlessError, send_pdf
from ..devices import PrinterError
from ..models import Delivery, DeliveryKind, DeliveryStatus, JobStatus, utcnow
from .. import quotas
from ..pdf import build_pdf, count_pages, detect_office, detect_type, page_range_count
from .runner import JobCancelled, JobContext

log = logging.getLogger(__name__)


# --- print -------------------------------------------------------------------

async def convert_document(ctx: JobContext) -> None:
    """Office documents become PDF through Gotenberg; other files pass through."""
    job = ctx.job()
    src = Path(job.file_path)
    with src.open("rb") as fh:
        head = fh.read(4096)
    if not detect_office(head, src.name):
        return
    out = ctx.job_dir / "converted.pdf"
    await asyncio.to_thread(
        convert_office, ctx.settings.gotenberg_url, src, out, ctx.settings.gotenberg_timeout,
        ctx.transports.get("gotenberg"),
    )
    ctx.update(file_path=str(out))


async def count_print_pages(ctx: JobContext) -> None:
    """Count pages ourselves; CUPS counts are unreliable for driverless printers."""
    job = ctx.job()
    path = Path(job.file_path)
    with path.open("rb") as fh:
        file_type = detect_type(fh.read(4096))
    total = await asyncio.to_thread(count_pages, path, file_type or "")
    pages = page_range_count(job.page_range, total) if total else None
    ctx.update(pages=pages)
    # Quota check now that the real page count is known (text files: counted after).
    user = ctx.db.get_user_by_name(job.user)
    if user is not None and pages:
        try:
            quotas.check(ctx.db, user, pages * max(job.copies, 1), ctx.settings.quota_mode,
                         exclude_job=job.id)
        except quotas.QuotaExceeded as exc:
            ctx.update(pages=None)  # nothing was printed; keep it out of the totals
            raise exc


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


# --- scan --------------------------------------------------------------------

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


async def run_deliveries(ctx: JobContext) -> None:
    """Send the PDF to every pending delivery. Failures are recorded per delivery
    and do not fail the job: the PDF stays available for download."""
    for delivery in ctx.db.list_deliveries(ctx.job_id):
        if delivery.status == DeliveryStatus.PENDING:
            await deliver(ctx, delivery.id)


async def deliver(ctx: JobContext, delivery_id: str) -> Delivery:
    db = ctx.db
    current = db.get_delivery(delivery_id)
    delivery = db.update_delivery(delivery_id, status=DeliveryStatus.SENDING, error=None,
                                  result=None, result_url=None, attempts=current.attempts + 1)
    job = ctx.job()
    try:
        if not job.file_path or job.files_purged or not Path(job.file_path).is_file():
            raise EmailError("The PDF is no longer available")
        pdf = Path(job.file_path)
        options = json.loads(delivery.options or "{}")
        if delivery.kind == DeliveryKind.EMAIL:
            subject = f"Scan: {options.get('title') or job.filename}"
            body = (f"Scanned with Platen on {job.created_at:%Y-%m-%d %H:%M} UTC "
                    f"({job.pages} page{'s' if job.pages != 1 else ''}, {job.dpi} DPI).\n")
            result = await asyncio.to_thread(
                send_pdf, ctx.settings, delivery.target, pdf, subject, body
            )
            url = None
        elif delivery.kind == DeliveryKind.PAPERLESS:
            result, url = await asyncio.to_thread(_paperless_upload, ctx, job.user, pdf, options)
        else:
            raise ValueError(f"unknown delivery kind {delivery.kind}")
    except Exception as exc:  # noqa: BLE001 - a delivery must never fail the job
        log.warning("delivery %s (%s) for job %s failed: %s", delivery_id, delivery.kind,
                    job.id, exc)
        return db.update_delivery(delivery_id, status=DeliveryStatus.FAILED,
                                  error=str(exc)[:1000], finished_at=utcnow())
    return db.update_delivery(delivery_id, status=DeliveryStatus.DONE, result=result,
                              result_url=url, finished_at=utcnow())


def _paperless_upload(ctx: JobContext, username: str, pdf: Path,
                      options: dict) -> tuple[str, str | None]:
    user = ctx.db.get_user_by_name(username)
    token = (user.paperless_token if user else "") or ctx.settings.paperless_token
    with PaperlessClient(ctx.settings.paperless_url, token,
                         transport=ctx.transports.get("paperless")) as client:
        task_id = client.upload(
            pdf, title=options.get("title") or "", tags=options.get("tags") or [],
            correspondent=options.get("correspondent"),
            document_type=options.get("document_type"),
        )
        result = client.wait(task_id, ctx.settings.paperless_wait,
                             interval=ctx.settings.poll_interval)
        if result.status == "SUCCESS":
            if result.document_id:
                return (f"Added to Paperless as document #{result.document_id}",
                        client.document_url(result.document_id))
            return "Added to Paperless", None
        if result.status in ("FAILURE", "REVOKED"):
            raise PaperlessError(result.message or "Paperless could not consume the document")
        return f"Accepted by Paperless, still processing (task {task_id})", None


PIPELINES = {
    "print": [convert_document, count_print_pages, submit_print, wait_print],
    "scan": [build_scan_pdf, run_deliveries],
}

RESUME_PRINT = [wait_print]
