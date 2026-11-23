"""JSON API. Same operations as the UI; authenticate with the session cookie or
``Authorization: Bearer <PLATEN_PASSWORD>`` when a password is set."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..models import Job, ScanPage
from ..services import Services

router = APIRouter(prefix="/api", tags=["api"])


def _svc(request: Request) -> Services:
    return request.app.state.services


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


@router.get("/printers")
async def printers(request: Request):
    return [asdict(p) for p in await _svc(request).list_printers()]


@router.post("/printers/sync")
async def sync_printers(request: Request):
    return {"messages": await _svc(request).sync_printers()}


@router.get("/scanners")
async def scanners(request: Request, refresh: bool = False):
    return [asdict(s) for s in await _svc(request).list_scanners(refresh=refresh)]


@router.get("/scanners/capabilities")
async def scanner_capabilities(request: Request, device: str):
    svc = _svc(request)
    await svc.get_scanner(device)
    return asdict(await svc.scanner_caps(device))


@router.get("/jobs")
async def jobs(request: Request, kind: str | None = None, status: str | None = None,
               limit: int = 100):
    return [job_out(j) for j in _svc(request).db.list_jobs(kind, status, min(limit, 1000))]


@router.get("/jobs/{job_id}")
async def job(request: Request, job_id: str):
    return job_out(_svc(request).get_job(job_id))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str):
    return job_out(await _svc(request).cancel_job(job_id))


@router.get("/jobs/{job_id}/file")
async def job_file(request: Request, job_id: str):
    job, path = _svc(request).job_file(job_id)
    return FileResponse(path, filename=job.filename or path.name)


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
    job = await _svc(request).create_print_job(file, printer, copies, duplex, color_mode,
                                               page_range)
    return job_out(job)


class ScanStart(BaseModel):
    scanner: str
    dpi: int | None = None
    mode: str | None = None


@router.post("/scan", status_code=201)
async def start_scan(request: Request, body: ScanStart):
    svc = _svc(request)
    caps = await svc.scanner_caps((await svc.get_scanner(body.scanner)).id)
    job = await svc.create_scan_session(
        body.scanner, body.dpi or caps.default_resolution(), body.mode or caps.default_mode()
    )
    return job_out(job)


@router.get("/scan/{job_id}/pages")
async def scan_pages(request: Request, job_id: str):
    svc = _svc(request)
    svc.get_job(job_id)
    return [page_out(job_id, p) for p in svc.db.list_pages(job_id)]


@router.post("/scan/{job_id}/pages", status_code=201)
async def scan_page(request: Request, job_id: str):
    return page_out(job_id, await _svc(request).scan_page(job_id))


@router.delete("/scan/{job_id}/pages/{page_id}", status_code=204)
async def delete_page(request: Request, job_id: str, page_id: str):
    _svc(request).delete_page(job_id, page_id)


@router.post("/scan/{job_id}/finish", status_code=202)
async def finish_scan(request: Request, job_id: str):
    return job_out(await _svc(request).finish_scan(job_id))
