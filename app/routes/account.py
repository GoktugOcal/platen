"""The logged-in user's own account: password, email, Paperless token, API token."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request

from ..auth import set_session_cookie
from ..users import UserError
from .ui import current_user, redirect, render

router = APIRouter(include_in_schema=False)


def _account(request: Request, status_code: int = 200, message: str | None = None,
             error: str | None = None, new_token: str | None = None):
    return render(request, "account.html", status_code=status_code, message=message,
                  error=error, new_token=new_token)


@router.get("/account")
async def account_page(request: Request):
    return _account(request)


@router.post("/account/profile")
async def account_profile(request: Request, email: str = Form(""),
                          paperless_token: str = Form(None)):
    users, user = request.app.state.users, current_user(request)
    try:
        request.state.user = users.update(user.id, email=email, paperless_token=paperless_token)
    except UserError as exc:
        return _account(request, exc.status_code, error=exc.message)
    return _account(request, message="Saved")


@router.post("/account/password")
async def account_password(request: Request, current: str = Form(""), new: str = Form(""),
                           new2: str = Form("")):
    users, user = request.app.state.users, current_user(request)
    try:
        if new != new2:
            raise UserError("The new passwords do not match")
        user = await asyncio.to_thread(users.change_own_password, user, current, new)
    except UserError as exc:
        return _account(request, exc.status_code, error=exc.message)
    # Other sessions are now logged out; keep this one.
    request.state.user = user
    response = _account(request, message="Password changed. Other sessions were logged out.")
    set_session_cookie(response, request, request.app.state.sessions, user)
    return response


@router.post("/account/token")
async def account_token(request: Request, action: str = Form("new")):
    users, user = request.app.state.users, current_user(request)
    if action == "revoke":
        users.revoke_api_token(user.id)
        request.state.user = users.get(user.id)
        return _account(request, message="API token revoked")
    token = users.issue_api_token(user.id)
    request.state.user = users.get(user.id)
    return _account(request, new_token=token)


@router.post("/account/logout-everywhere")
async def account_logout_everywhere(request: Request):
    request.app.state.users.logout_everywhere(current_user(request).id)
    return redirect("/login")
