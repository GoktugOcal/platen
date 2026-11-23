"""JSON API. Same operations as the UI.

Authenticate with the session cookie or ``Authorization: Bearer <api token>``
(create a token on the Account page).
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..models import Delivery, Job, Role, ScanPage, User
from ..services import DeliveryRequest, PaperlessRequest, ServiceError, Services

router = APIRouter(prefix="/api", tags=["api"])


def _svc(request: Request) -> Services:
    return request.app.state.services


def _user(request: Request) -> User:
    return request.state.user


def _admin(request: Request) -> User:
    user = _user(request)
    if not user.is_admin:
        raise ServiceError("Only admins can do this", 403)
    return user


def job_out(job: Job) -> dict:
    data = job.model_dump(mode="json", exclude={"file_path"})
    data["has_file"] = bool(job.file_path) and not job.files_purged
    return data


def page_out(job_id: str, page: ScanPage) -> dict:
    return {
        "id": page.id,
        "position": page.position,
        "created_at": page.created_at.isoformat(),
        "thumbnail_url": f"/scan/{job_id}/pages/{page.id}/thumb",
    }


def user_out(user: User) -> dict:
    return {
        "id": user.id, "username": user.username, "role": user.role, "email": user.email,
        "active": user.active, "allowed_printers": user.printer_list,
        "has_api_token": bool(user.api_token_hash),
        "has_paperless_token": bool(user.paperless_token),
        "created_at": user.created_at.isoformat(),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def delivery_out(d: Delivery) -> dict:
    return d.model_dump(mode="json")


# --- me / users ----------------------------------------------------------------

@router.get("/me")
async def me(request: Request):
    return user_out(_user(request))


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = Role.USER
    email: str = ""
    allowed_printers: list[str] = Field(default_factory=list)


class UserUpdate(BaseModel):
    role: str | None = None
    email: str | None = None
    active: bool | None = None
    allowed_printers: list[str] | None = None
    password: str | None = None


@router.get("/users")
async def users(request: Request):
    _admin(request)
    return [user_out(u) for u in request.app.state.db.list_users()]


@router.post("/users", status_code=201)
async def create_user(request: Request, body: UserCreate):
    _admin(request)
    user = await asyncio.to_thread(
        request.app.state.users.create, body.username, body.password, body.role, body.email,
        ",".join(body.allowed_printers),
    )
    return user_out(user)


@router.patch("/users/{user_id}")
async def update_user(request: Request, user_id: int, body: UserUpdate):
    _admin(request)
    users = request.app.state.users
    user = users.update(
        user_id, role=body.role, email=body.email, active=body.active,
        allowed_printers=(",".join(body.allowed_printers)
                          if body.allowed_printers is not None else None),
    )
    if body.password:
        user = await asyncio.to_thread(users.set_password, user_id, body.password)
    return user_out(user)


# --- devices -------------------------------------------------------------------

@router.get("/printers")
async def printers(request: Request):
    return [asdict(p) for p in await _svc(request).list_printers(_user(request))]


@router.post("/printers/sync")
async def sync_printers(request: Request):
    _admin(request)
    return {"messages": await _svc(request).sync_printers()}


@router.get("/scanners")
async def scanners(request: Request, refresh: bool = False):
    return [asdict(s) for s in await _svc(request).list_scanners(refresh=refresh)]


@router.get("/scanners/capabilities")
async def scanner_capabilities(request: Request, device: str):
    svc = _svc(request)
    await svc.get_scanner(device)
    return asdict(await svc.scanner_caps(device))


# --- jobs ----------------------------------------------------------------------

@router.get("/jobs")
async def jobs(request: Request, kind: str | None = None, status: str | None = None,
               limit: int = 100):
    return [job_out(j) for j in _svc(request).list_jobs(_user(request), kind, status,
                                                        min(limit, 1000))]


@router.get("/jobs/{job_id}")
async def job(request: Request, job_id: str):
    return job_out(_svc(request).get_job(job_id, _user(request)))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str):
    return job_out(await _svc(request).cancel_job(job_id, _user(request)))


@router.get("/jobs/{job_id}/file")
async def job_file(request: Request, job_id: str):
    job, path = _svc(request).job_file(job_id, _user(request))
    return FileResponse(path, filename=job.filename or path.name)


@router.get("/jobs/{job_id}/deliveries")
async def deliveries(request: Request, job_id: str):
    return [delivery_out(d) for d in _svc(request).list_deliveries(job_id, _user(request))]


@router.post("/jobs/{job_id}/deliveries/{delivery_id}/retry", status_code=202)
async def retry_delivery(request: Request, job_id: str, delivery_id: str):
    return delivery_out(await _svc(request).retry_delivery(job_id, delivery_id,
                                                           _user(request)))


# --- print ---------------------------------------------------------------------

@router.post("/print", status_code=202)
async def print_file(
    request: Request,
    file: UploadFile = File(...),
    printer: str = Form(...),
    copies: int = Form(1),
    duplex: str = Form("one-sided"),
    color_mode: str = Form(""),
    page_range: str = Form(""),
):
    job = await _svc(request).create_print_job(_user(request), file, printer, copies, duplex,
                                               color_mode, page_range)
    return job_out(job)


# --- scan ----------------------------------------------------------------------

class ScanStart(BaseModel):
    scanner: str
    dpi: int | None = None
    mode: str | None = None


class PaperlessOptions(BaseModel):
    title: str = ""
    tags: list[int] = Field(default_factory=list)
    correspondent: int | None = None
    document_type: int | None = None


class ScanFinish(BaseModel):
    email: str | None = None
    paperless: PaperlessOptions | None = None


@router.post("/scan", status_code=201)
async def start_scan(request: Request, body: ScanStart):
    svc = _svc(request)
    caps = await svc.scanner_caps((await svc.get_scanner(body.scanner)).id)
    job = await svc.create_scan_session(
        _user(request), body.scanner, body.dpi or caps.default_resolution(),
        body.mode or caps.default_mode(),
    )
    return job_out(job)


@router.get("/scan/{job_id}/pages")
async def scan_pages(request: Request, job_id: str):
    svc = _svc(request)
    svc.get_job(job_id, _user(request))
    return [page_out(job_id, p) for p in svc.db.list_pages(job_id)]


@router.post("/scan/{job_id}/pages", status_code=201)
async def scan_page(request: Request, job_id: str):
    return page_out(job_id, await _svc(request).scan_page(job_id, _user(request)))


@router.delete("/scan/{job_id}/pages/{page_id}", status_code=204)
async def delete_page(request: Request, job_id: str, page_id: str):
    _svc(request).delete_page(job_id, page_id, _user(request))


@router.post("/scan/{job_id}/finish", status_code=202)
async def finish_scan(request: Request, job_id: str, body: ScanFinish | None = None):
    body = body or ScanFinish()
    req = DeliveryRequest(
        email=body.email,
        paperless=PaperlessRequest(**body.paperless.model_dump()) if body.paperless else None,
    )
    return job_out(await _svc(request).finish_scan(job_id, _user(request), req))


@router.get("/paperless/metadata")
async def paperless_metadata(request: Request):
    return await _svc(request).paperless_metadata(_user(request))
