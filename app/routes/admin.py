"""User management for admins."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request

from ..auth import set_session_cookie
from ..models import Role
from ..users import UserError
from .ui import redirect, render, require_admin, svc

router = APIRouter(prefix="/admin", include_in_schema=False)


async def _users_page(request: Request, status_code: int = 200, error: str | None = None,
                      form: dict | None = None):
    users = request.app.state.users.db.list_users()
    return render(request, "admin_users.html", status_code=status_code,
                  users=users, quotas={u.id: svc(request).quota_status(u) for u in users},
                  printers=await svc(request).list_printers(), error=error, form=form or {})


@router.get("/users")
async def users_page(request: Request):
    require_admin(request)
    return await _users_page(request)


@router.post("/users")
async def users_create(request: Request, username: str = Form(""), password: str = Form(""),
                       role: str = Form(Role.USER), email: str = Form(""),
                       quota_pages: int = Form(0), quota_period: str = Form("month")):
    require_admin(request)
    form = await request.form()
    allowed = ",".join(form.getlist("allowed_printers"))
    try:
        await asyncio.to_thread(request.app.state.users.create, username, password, role, email,
                                allowed, quota_pages, quota_period)
    except UserError as exc:
        return await _users_page(request, exc.status_code, exc.message,
                                 {"username": username, "email": email, "role": role})
    return redirect("/admin/users")


async def _edit_page(request: Request, user_id: int, status_code: int = 200,
                     error: str | None = None, message: str | None = None):
    target = request.app.state.users.get(user_id)
    return render(request, "admin_user_form.html", status_code=status_code,
                  target=target, quota=svc(request).quota_status(target),
                  printers=await svc(request).list_printers(), error=error, message=message)


@router.get("/users/{user_id}")
async def user_edit_page(request: Request, user_id: int):
    require_admin(request)
    return await _edit_page(request, user_id)


@router.post("/users/{user_id}")
async def user_edit(request: Request, user_id: int, role: str = Form(Role.USER),
                    email: str = Form(""), active: str = Form(""), password: str = Form(""),
                    quota_pages: int = Form(0), quota_period: str = Form("month")):
    me = require_admin(request)
    users = request.app.state.users
    form = await request.form()
    try:
        updated = users.update(user_id, role=role, email=email, active=bool(active),
                               allowed_printers=",".join(form.getlist("allowed_printers")),
                               quota_pages=quota_pages, quota_period=quota_period)
        if password:
            updated = await asyncio.to_thread(users.set_password, user_id, password)
    except UserError as exc:
        return await _edit_page(request, user_id, exc.status_code, error=exc.message)
    if user_id == me.id:
        if not updated.is_admin or not updated.active:
            return redirect("/")
        request.state.user = updated
    response = await _edit_page(request, user_id, message="Saved")
    if user_id == me.id and password:
        set_session_cookie(response, request, request.app.state.sessions, updated)
    return response
