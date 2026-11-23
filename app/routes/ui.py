"""Server-rendered UI (Jinja2 + HTMX)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..auth import COOKIE_NAME, SESSION_MAX_AGE
from ..models import ACTIVE_STATUSES, JobKind, JobStatus
from ..services import COLOR_OPTIONS, DUPLEX_OPTIONS, ServiceError, Services

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


def _localtime(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _filesize(value: int | None) -> str:
    if value is None:
        return ""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


templates.env.filters["localtime"] = _localtime
templates.env.filters["filesize"] = _filesize
templates.env.globals.update(
    DUPLEX_OPTIONS=DUPLEX_OPTIONS, COLOR_OPTIONS=COLOR_OPTIONS, JobStatus=JobStatus,
    ACTIVE_STATUSES=ACTIVE_STATUSES,
)


def _svc(request: Request) -> Services:
    return request.app.state.services


def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    context.setdefault("auth_enabled", request.app.state.auth.enabled)
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def render_error(request: Request, exc: ServiceError) -> HTMLResponse:
    return render(request, "error.html", status_code=exc.status_code, message=exc.message)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --- print -----------------------------------------------------------------

@router.get("/")
async def index():
    return _redirect("/print")


@router.get("/print")
async def print_page(request: Request, printer: str | None = None):
    svc = _svc(request)
    printers = await svc.list_printers()
    recent = svc.db.list_jobs(kind=JobKind.PRINT, limit=5)
    return render(request, "print.html", printers=printers, selected=printer, recent=recent,
                  form={}, error=None)


@router.post("/print")
async def print_submit(
    request: Request,
    file: UploadFile = File(None),
    printer: str = Form(""),
    copies: int = Form(1),
    duplex: str = Form("one-sided"),
    color_mode: str = Form(""),
    page_range: str = Form(""),
):
    svc = _svc(request)
    try:
        job = await svc.create_print_job(file, printer, copies, duplex, color_mode, page_range)
    except ServiceError as exc:
        printers = await svc.list_printers()
        form = {"copies": copies, "duplex": duplex, "color_mode": color_mode,
                "page_range": page_range}
        return render(request, "print.html", status_code=exc.status_code, printers=printers,
                      selected=printer, recent=svc.db.list_jobs(kind=JobKind.PRINT, limit=5),
                      form=form, error=exc.message)
    return _redirect(f"/jobs/{job.id}")


# --- scan ------------------------------------------------------------------

@router.get("/scan")
async def scan_page(request: Request, device: str | None = None, refresh: bool = False):
    svc = _svc(request)
    scanners = await svc.list_scanners(refresh=refresh)
    selected = next((s for s in scanners if s.id == device), scanners[0] if scanners else None)
    caps, error = None, None
    if selected:
        try:
            caps = await svc.scanner_caps(selected.id)
        except ServiceError as exc:
            error = exc.message
    sessions = svc.db.list_jobs(kind=JobKind.SCAN, status=JobStatus.OPEN, limit=10)
    return render(request, "scan.html", scanners=scanners, selected=selected, caps=caps,
                  error=error, sessions=sessions)


@router.post("/scan")
async def scan_start(request: Request, device: str = Form(...), dpi: int = Form(...),
                     mode: str = Form(...)):
    job = await _svc(request).create_scan_session(device, dpi, mode)
    return _redirect(f"/scan/{job.id}")


@router.get("/scan/{job_id}")
async def scan_session(request: Request, job_id: str):
    svc = _svc(request)
    job = svc.get_job(job_id)
    if job.kind != JobKind.SCAN:
        raise ServiceError("Not a scan job", 404)
    if job.status != JobStatus.OPEN:
        return _redirect(f"/jobs/{job_id}")
    return render(request, "scan_session.html", job=job, pages=svc.db.list_pages(job_id),
                  error=None)


async def _pages_fragment(request: Request, job_id: str, action) -> HTMLResponse:
    svc = _svc(request)
    error = None
    try:
        result = action()
        if asyncio.iscoroutine(result):
            await result
    except ServiceError as exc:
        error = exc.message
    return render(request, "_scan_pages.html", job=svc.get_job(job_id),
                  pages=svc.db.list_pages(job_id), error=error)


@router.post("/scan/{job_id}/pages")
async def scan_add_page(request: Request, job_id: str):
    return await _pages_fragment(request, job_id, lambda: _svc(request).scan_page(job_id))


@router.post("/scan/{job_id}/pages/{page_id}/rescan")
async def scan_rescan_page(request: Request, job_id: str, page_id: str):
    return await _pages_fragment(
        request, job_id, lambda: _svc(request).scan_page(job_id, replace_page_id=page_id)
    )


@router.post("/scan/{job_id}/pages/{page_id}/delete")
async def scan_delete_page(request: Request, job_id: str, page_id: str):
    return await _pages_fragment(
        request, job_id, lambda: _svc(request).delete_page(job_id, page_id)
    )


@router.get("/scan/{job_id}/pages/{page_id}/thumb")
async def scan_thumb(request: Request, job_id: str, page_id: str):
    path = _svc(request).page_thumb(job_id, page_id)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.post("/scan/{job_id}/finish")
async def scan_finish(request: Request, job_id: str):
    svc = _svc(request)
    try:
        await svc.finish_scan(job_id)
    except ServiceError as exc:
        return render(request, "scan_session.html", status_code=exc.status_code,
                      job=svc.get_job(job_id), pages=svc.db.list_pages(job_id),
                      error=exc.message)
    return _redirect(f"/jobs/{job_id}")


@router.post("/scan/{job_id}/discard")
async def scan_discard(request: Request, job_id: str):
    await _svc(request).cancel_job(job_id)
    return _redirect("/scan")


# --- jobs ------------------------------------------------------------------

@router.get("/jobs")
async def jobs_page(request: Request, kind: str = "", status: str = ""):
    jobs = _svc(request).db.list_jobs(kind or None, status or None, limit=200)
    query = urlencode({k: v for k, v in {"kind": kind, "status": status}.items() if v})
    polling = any(j.status in ACTIVE_STATUSES and j.status != JobStatus.OPEN for j in jobs)
    return render(request, "jobs.html", jobs=jobs, kind=kind, status=status, query=query,
                  polling=polling, statuses=[s.value for s in JobStatus])


@router.get("/jobs/{job_id}")
async def job_page(request: Request, job_id: str):
    svc = _svc(request)
    job = svc.get_job(job_id)
    pages = svc.db.list_pages(job_id) if job.kind == JobKind.SCAN else []
    return render(request, "job.html", job=job, pages=pages)


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(request: Request, job_id: str):
    await _svc(request).cancel_job(job_id)
    return _redirect(f"/jobs/{job_id}")


@router.get("/jobs/{job_id}/file")
async def job_file(request: Request, job_id: str):
    job, path = _svc(request).job_file(job_id)
    return FileResponse(path, filename=job.filename or path.name)


# --- devices ---------------------------------------------------------------

@router.get("/devices")
async def devices_page(request: Request):
    svc = _svc(request)
    printers, scanners = await asyncio.gather(svc.list_printers(), svc.list_scanners())
    return render(request, "devices.html", printers=printers, scanners=scanners,
                  messages=svc.device_messages)


@router.post("/devices/refresh")
async def devices_refresh(request: Request):
    svc = _svc(request)
    await asyncio.gather(svc.sync_printers(), svc.list_scanners(refresh=True))
    return _redirect("/devices")


# --- login -----------------------------------------------------------------

def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


@router.get("/login")
async def login_page(request: Request, next: str = "/"):
    if not request.app.state.auth.enabled:
        return _redirect("/")
    return render(request, "login.html", next=_safe_next(next), error=None)


@router.post("/login")
async def login_submit(request: Request, password: str = Form(""), next: str = Form("/")):
    auth = request.app.state.auth
    if not auth.enabled:
        return _redirect("/")
    if not auth.check_password(password):
        await asyncio.sleep(1)  # slow down guessing
        return render(request, "login.html", status_code=401, next=_safe_next(next),
                      error="Wrong password")
    response = _redirect(_safe_next(next))
    response.set_cookie(
        COOKIE_NAME, auth.make_cookie(), max_age=SESSION_MAX_AGE, httponly=True,
        samesite="lax", secure=request.url.scheme == "https",
    )
    return response


@router.post("/logout")
async def logout():
    response = _redirect("/login")
    response.delete_cookie(COOKIE_NAME)
    return response
