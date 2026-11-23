"""Server-rendered UI (Jinja2 + HTMX)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..auth import COOKIE_NAME, set_session_cookie
from ..models import ACTIVE_STATUSES, DeliveryStatus, JobKind, JobStatus, Role, User
from ..services import (
    COLOR_OPTIONS,
    DUPLEX_OPTIONS,
    DeliveryRequest,
    PaperlessRequest,
    ServiceError,
    Services,
)
from ..users import UserError

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
    ACTIVE_STATUSES=ACTIVE_STATUSES, DeliveryStatus=DeliveryStatus,
)


def svc(request: Request) -> Services:
    return request.app.state.services


def current_user(request: Request) -> User:
    return request.state.user


def require_admin(request: Request) -> User:
    user = current_user(request)
    if not user.is_admin:
        raise ServiceError("Only admins can do this", 403)
    return user


def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    settings = request.app.state.settings
    context.setdefault("user", getattr(request.state, "user", None))
    context.setdefault("features", {
        "email": settings.email_enabled, "paperless": settings.paperless_enabled,
        "office": settings.office_enabled,
    })
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def render_error(request: Request, message: str, status_code: int) -> HTMLResponse:
    return render(request, "error.html", status_code=status_code, message=message)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# --- print -----------------------------------------------------------------

@router.get("/")
async def index():
    return redirect("/print")


async def _print_page(request: Request, status_code: int = 200, selected: str | None = None,
                      form: dict | None = None, error: str | None = None) -> HTMLResponse:
    s, user = svc(request), current_user(request)
    return render(request, "print.html", status_code=status_code,
                  printers=await s.list_printers(user), selected=selected,
                  recent=s.list_jobs(user, kind=JobKind.PRINT, limit=5),
                  accept=",".join(s.accepted_upload_types), form=form or {}, error=error)


@router.get("/print")
async def print_page(request: Request, printer: str | None = None):
    return await _print_page(request, selected=printer)


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
    try:
        job = await svc(request).create_print_job(current_user(request), file, printer, copies,
                                                  duplex, color_mode, page_range)
    except ServiceError as exc:
        form = {"copies": copies, "duplex": duplex, "color_mode": color_mode,
                "page_range": page_range}
        return await _print_page(request, exc.status_code, printer, form, exc.message)
    return redirect(f"/jobs/{job.id}")


# --- scan ------------------------------------------------------------------

@router.get("/scan")
async def scan_page(request: Request, device: str | None = None, refresh: bool = False):
    s = svc(request)
    scanners = await s.list_scanners(refresh=refresh)
    selected = next((x for x in scanners if x.id == device), scanners[0] if scanners else None)
    caps, error = None, None
    if selected:
        try:
            caps = await s.scanner_caps(selected.id)
        except ServiceError as exc:
            error = exc.message
    return render(request, "scan.html", scanners=scanners, selected=selected, caps=caps,
                  error=error, sessions=s.open_sessions(current_user(request)))


@router.post("/scan")
async def scan_start(request: Request, device: str = Form(...), dpi: int = Form(...),
                     mode: str = Form(...)):
    job = await svc(request).create_scan_session(current_user(request), device, dpi, mode)
    return redirect(f"/scan/{job.id}")


def _session_page(request: Request, job_id: str, status_code: int = 200,
                  error: str | None = None) -> HTMLResponse:
    s, user = svc(request), current_user(request)
    job = s.get_job(job_id, user)
    return render(request, "scan_session.html", status_code=status_code, job=job,
                  pages=s.db.list_pages(job_id), error=error,
                  default_email=user.email,
                  paperless_ready=bool(user.paperless_token
                                       or request.app.state.settings.paperless_token))


@router.get("/scan/{job_id}")
async def scan_session(request: Request, job_id: str):
    job = svc(request).get_job(job_id, current_user(request))
    if job.kind != JobKind.SCAN:
        raise ServiceError("Not a scan job", 404)
    if job.status != JobStatus.OPEN:
        return redirect(f"/jobs/{job_id}")
    return _session_page(request, job_id)


async def _pages_fragment(request: Request, job_id: str, action) -> HTMLResponse:
    s, user = svc(request), current_user(request)
    error = None
    try:
        result = action()
        if asyncio.iscoroutine(result):
            await result
    except ServiceError as exc:
        error = exc.message
    return render(request, "_scan_pages.html", job=s.get_job(job_id, user),
                  pages=s.db.list_pages(job_id), error=error)


@router.post("/scan/{job_id}/pages")
async def scan_add_page(request: Request, job_id: str):
    user = current_user(request)
    return await _pages_fragment(request, job_id,
                                 lambda: svc(request).scan_page(job_id, user))


@router.post("/scan/{job_id}/pages/{page_id}/rescan")
async def scan_rescan_page(request: Request, job_id: str, page_id: str):
    user = current_user(request)
    return await _pages_fragment(
        request, job_id, lambda: svc(request).scan_page(job_id, user, replace_page_id=page_id)
    )


@router.post("/scan/{job_id}/pages/{page_id}/delete")
async def scan_delete_page(request: Request, job_id: str, page_id: str):
    user = current_user(request)
    return await _pages_fragment(request, job_id,
                                 lambda: svc(request).delete_page(job_id, page_id, user))


@router.get("/scan/{job_id}/pages/{page_id}/thumb")
async def scan_thumb(request: Request, job_id: str, page_id: str):
    path = svc(request).page_thumb(job_id, page_id, current_user(request))
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.get("/scan/{job_id}/paperless-fields")
async def scan_paperless_fields(request: Request, job_id: str):
    """Lazy-loaded so a slow Paperless never delays the scan page."""
    s, user = svc(request), current_user(request)
    s.get_job(job_id, user)
    try:
        meta, error = await s.paperless_metadata(user), None
    except ServiceError as exc:
        meta, error = None, exc.message
    return render(request, "_paperless_fields.html", meta=meta, error=error)


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.post("/scan/{job_id}/finish")
async def scan_finish(request: Request, job_id: str):
    form = await request.form()
    req = DeliveryRequest()
    if form.get("deliver_email"):
        req.email = str(form.get("email", "")).strip()
        if not req.email:
            return _session_page(request, job_id, 400, "Enter an email address")
    if form.get("deliver_paperless"):
        req.paperless = PaperlessRequest(
            title=str(form.get("pl_title", "")),
            tags=[t for t in (_int_or_none(v) for v in form.getlist("pl_tags")) if t],
            correspondent=_int_or_none(form.get("pl_correspondent")),
            document_type=_int_or_none(form.get("pl_document_type")),
        )
    try:
        await svc(request).finish_scan(job_id, current_user(request), req)
    except ServiceError as exc:
        return _session_page(request, job_id, exc.status_code, exc.message)
    return redirect(f"/jobs/{job_id}")


@router.post("/scan/{job_id}/discard")
async def scan_discard(request: Request, job_id: str):
    await svc(request).cancel_job(job_id, current_user(request))
    return redirect("/scan")


# --- jobs ------------------------------------------------------------------

@router.get("/jobs")
async def jobs_page(request: Request, kind: str = "", status: str = ""):
    jobs = svc(request).list_jobs(current_user(request), kind or None, status or None)
    query = urlencode({k: v for k, v in {"kind": kind, "status": status}.items() if v})
    polling = any(j.status in ACTIVE_STATUSES and j.status != JobStatus.OPEN for j in jobs)
    return render(request, "jobs.html", jobs=jobs, kind=kind, status=status, query=query,
                  polling=polling, statuses=[s.value for s in JobStatus])


@router.get("/jobs/{job_id}")
async def job_page(request: Request, job_id: str):
    s, user = svc(request), current_user(request)
    job = s.get_job(job_id, user)
    deliveries = s.list_deliveries(job_id, user)
    return render(request, "job.html", job=job, deliveries=deliveries)


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(request: Request, job_id: str):
    await svc(request).cancel_job(job_id, current_user(request))
    return redirect(f"/jobs/{job_id}")


@router.post("/jobs/{job_id}/deliveries/{delivery_id}/retry")
async def delivery_retry(request: Request, job_id: str, delivery_id: str):
    await svc(request).retry_delivery(job_id, delivery_id, current_user(request))
    return redirect(f"/jobs/{job_id}")


@router.get("/jobs/{job_id}/file")
async def job_file(request: Request, job_id: str):
    job, path = svc(request).job_file(job_id, current_user(request))
    return FileResponse(path, filename=job.filename or path.name)


# --- devices ---------------------------------------------------------------

@router.get("/devices")
async def devices_page(request: Request):
    s = svc(request)
    printers, scanners = await asyncio.gather(s.list_printers(), s.list_scanners())
    return render(request, "devices.html", printers=printers, scanners=scanners,
                  messages=s.device_messages)


@router.post("/devices/refresh")
async def devices_refresh(request: Request):
    require_admin(request)
    s = svc(request)
    await asyncio.gather(s.sync_printers(), s.list_scanners(refresh=True))
    return redirect("/devices")


# --- login, logout, first-run setup ----------------------------------------

def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


@router.get("/login")
async def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=_safe_next(next), error=None, username="")


@router.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                       next: str = Form("/")):
    state = request.app.state
    keys = (f"ip:{request.client.host if request.client else '?'}",
            f"user:{username.strip().lower()}")
    delay = state.login_throttle.delay(*keys)
    if delay:
        await asyncio.sleep(delay)
    user = await asyncio.to_thread(state.users.authenticate, username, password)
    if user is None:
        state.login_throttle.failed(*keys)
        await asyncio.sleep(0.5)
        return render(request, "login.html", status_code=401, next=_safe_next(next),
                      error="Wrong username or password", username=username)
    state.login_throttle.succeeded(*keys)
    response = redirect(_safe_next(next))
    set_session_cookie(response, request, state.sessions, user)
    return response


@router.post("/logout")
async def logout():
    response = redirect("/login")
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/setup")
async def setup_page(request: Request):
    return render(request, "setup.html", error=None, form={})


@router.post("/setup")
async def setup_submit(request: Request, username: str = Form(""), password: str = Form(""),
                       password2: str = Form(""), email: str = Form("")):
    users = request.app.state.users
    form = {"username": username, "email": email}
    if not users.needs_setup:
        return redirect("/login")
    try:
        if password != password2:
            raise UserError("The passwords do not match")
        user = await asyncio.to_thread(users.create, username, password, Role.ADMIN, email)
    except UserError as exc:
        return render(request, "setup.html", status_code=exc.status_code, error=exc.message,
                      form=form)
    response = redirect("/")
    set_session_cookie(response, request, request.app.state.sessions, user)
    return response
