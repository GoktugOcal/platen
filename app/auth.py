"""Session cookies, API tokens and the authentication middleware."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from .models import User

COOKIE_NAME = "platen_session"
SESSION_MAX_AGE = 30 * 24 * 3600
PUBLIC_PREFIXES = ("/static/", "/login", "/healthz")
SETUP_PATH = "/setup"


def load_secret_key(configured: str, data_dir: Path) -> str:
    if configured:
        return configured
    path = data_dir / "secret_key"
    if path.is_file():
        return path.read_text().strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    return key


class Sessions:
    def __init__(self, secret_key: str):
        self.serializer = URLSafeTimedSerializer(secret_key, salt="platen-session-v2")

    def make_cookie(self, user: User) -> str:
        return self.serializer.dumps({"uid": user.id, "epoch": user.session_epoch})

    def read_cookie(self, value: str) -> tuple[int, int] | None:
        try:
            data = self.serializer.loads(value, max_age=SESSION_MAX_AGE)
            return int(data["uid"]), int(data["epoch"])
        except (BadSignature, KeyError, TypeError, ValueError):
            return None


def set_session_cookie(response: Response, request: Request, sessions: Sessions,
                       user: User) -> None:
    response.set_cookie(
        COOKIE_NAME, sessions.make_cookie(user), max_age=SESSION_MAX_AGE, httponly=True,
        samesite="lax", secure=request.url.scheme == "https",
    )


def resolve_user(request: Request) -> User | None:
    state = request.app.state
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return state.users.from_api_token(header[7:].strip())
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    parsed = state.sessions.read_cookie(cookie)
    if parsed is None:
        return None
    uid, epoch = parsed
    user = state.db.get_user(uid)
    if user is None or not user.active or user.session_epoch != epoch:
        return None
    return user


class AuthMiddleware(BaseHTTPMiddleware):
    """Every request needs a logged-in user, except public paths and first-run setup."""

    def __init__(self, app):
        super().__init__(app)
        self._setup_done = False

    def _needs_setup(self, request: Request) -> bool:
        if self._setup_done:
            return False
        if request.app.state.users.needs_setup:
            return True
        self._setup_done = True  # users can be deactivated but never deleted
        return False

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        if self._needs_setup(request):
            if path == SETUP_PATH:
                return await call_next(request)
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Create the first admin at /setup"},
                                    status_code=503)
            return RedirectResponse(SETUP_PATH, status_code=303)
        if path == SETUP_PATH:
            return RedirectResponse("/", status_code=303)

        user = resolve_user(request)
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            if request.headers.get("hx-request"):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        request.state.user = user
        return await call_next(request)
