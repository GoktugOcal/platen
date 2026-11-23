"""Optional single-password authentication (MVP; OIDC comes in v0.2)."""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

COOKIE_NAME = "platen_session"
SESSION_MAX_AGE = 30 * 24 * 3600
PUBLIC_PREFIXES = ("/static/", "/login", "/healthz")


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


class Authenticator:
    def __init__(self, password: str, secret_key: str):
        self.password = password
        self.serializer = URLSafeTimedSerializer(secret_key, salt="platen-session")

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def check_password(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self.password.encode())

    def make_cookie(self) -> str:
        return self.serializer.dumps({"user": "admin"})

    def user_from_request(self, request: Request) -> str | None:
        if not self.enabled:
            return "admin"
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer ") and self.check_password(header[7:].strip()):
            return "admin"
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie:
            try:
                return self.serializer.loads(cookie, max_age=SESSION_MAX_AGE)["user"]
            except (BadSignature, KeyError, TypeError):
                return None
        return None


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, authenticator: Authenticator):
        super().__init__(app)
        self.auth = authenticator

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not self.auth.enabled or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        user = self.auth.user_from_request(request)
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            if request.headers.get("hx-request"):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        request.state.user = user
        return await call_next(request)
