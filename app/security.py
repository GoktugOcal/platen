"""Browser-facing protections: cross-site request blocking, rate limits, early
upload size checks and security headers.

Cross-site request forgery is blocked with the browser's own request metadata
instead of per-form tokens (the approach of Go's ``http.CrossOriginProtection``):
every modern browser sends ``Sec-Fetch-Site`` on each request, and older ones send
``Origin`` on cross-origin POSTs. Requests without either header come from
non-browser clients (curl, scripts using API tokens), which cannot carry a
victim's session cookie, so they are allowed.
"""
from __future__ import annotations

import re
import time
from collections import deque
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    # Inline style attributes are used for widths and chart colours; styles cannot run code.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "connect-src 'self'",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
# FastAPI's interactive API docs load their assets from a CDN.
DOCS_CSP = "frame-ancestors 'none'"
DOCS_PATHS = ("/docs", "/redoc")

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "cross-origin-opener-policy": "same-origin",
}


def cross_origin_problem(method: str, headers: Headers,
                         trusted_origins: frozenset[str] = frozenset()) -> str | None:
    """Why an unsafe request looks cross-site, or None if it is fine."""
    if method in SAFE_METHODS:
        return None
    origin = headers.get("origin")
    if origin and origin.rstrip("/").lower() in trusted_origins:
        return None
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site:
        # "none" = typed URL / bookmark; "same-site" (another subdomain) is still blocked.
        return None if fetch_site in ("same-origin", "none") else f"Sec-Fetch-Site: {fetch_site}"
    if not origin:
        return None  # not a browser
    if origin == "null":
        return "Origin: null"
    if urlsplit(origin).netloc.lower() == headers.get("host", "").lower():
        return None
    return f"Origin {origin} does not match Host"


class SecurityMiddleware:
    """Outermost layer: blocks cross-site writes and oversized bodies before any
    work is done, and adds security headers to every response."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int,
                 trusted_origins: frozenset[str] = frozenset()):
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.trusted_origins = trusted_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        path: str = scope["path"]
        method: str = scope["method"]

        problem = cross_origin_problem(method, headers, self.trusted_origins)
        if problem:
            response = _error(path, 403, "Cross-site request blocked. Reload the page and try "
                                         "again from Platen itself.")
            await self._send_with_headers(response, scope, receive, send, path)
            return

        length = headers.get("content-length")
        if self.max_body_bytes and length and length.isdigit() \
                and int(length) > self.max_body_bytes:
            limit_mb = self.max_body_bytes // (1024 * 1024)
            response = _error(path, 413, f"Request is too large (limit about {limit_mb} MB).")
            await self._send_with_headers(response, scope, receive, send, path)
            return

        await self._send_with_headers(self.app, scope, receive, send, path)

    @staticmethod
    async def _send_with_headers(app: ASGIApp, scope: Scope, receive: Receive, send: Send,
                                 path: str) -> None:
        csp = DOCS_CSP if path.startswith(DOCS_PATHS) else CSP

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                headers.setdefault("content-security-policy", csp)
            await send(message)

        await app(scope, receive, send_wrapper)


def _error(path: str, status: int, message: str) -> Response:
    if path.startswith("/api/"):
        return JSONResponse({"detail": message}, status_code=status)
    return PlainTextResponse(message, status_code=status)


class RateLimiter:
    """Sliding-window request counter per key, in memory (one process)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._calls = 0

    def hit(self, key: str, limit: int, window: float = 60.0,
            now: float | None = None) -> float:
        """Record a request. Returns 0 if allowed, else seconds until a retry can succeed."""
        if limit <= 0:
            return 0.0
        now = time.monotonic() if now is None else now
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= limit:
            return max(hits[0] + window - now, 0.1)
        hits.append(now)
        self._calls += 1
        if self._calls % 1000 == 0:
            self._prune(now - window)
        return 0.0

    def _prune(self, cutoff: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            del self._hits[key]


# Writes that upload files, drive a device or start a job.
HEAVY_ROUTES = re.compile(
    r"^/(api/)?(print|scan|scan/[^/]+/(pages|finish|pages/[^/]+/rescan)"
    r"|jobs/[^/]+/deliveries/[^/]+/retry|printers/sync)$"
    r"|^/devices/refresh$"
)
UNLIMITED_PREFIXES = ("/static/", "/healthz")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Runs inside AuthMiddleware, so requests are counted per user when logged in
    (several people behind one proxy do not share a limit) and per IP otherwise."""

    def __init__(self, app: ASGIApp, *, requests_per_minute: int, heavy_per_minute: int):
        super().__init__(app)
        self.limiter = RateLimiter()
        self.requests_per_minute = requests_per_minute
        self.heavy_per_minute = heavy_per_minute

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path.startswith(UNLIMITED_PREFIXES):
            return await call_next(request)
        user = getattr(request.state, "user", None)
        key = f"user:{user.id}" if user is not None else \
            f"ip:{request.client.host if request.client else '?'}"
        wait = self.limiter.hit(key, self.requests_per_minute)
        if not wait and request.method not in SAFE_METHODS and HEAVY_ROUTES.match(path):
            wait = self.limiter.hit(key + ":heavy", self.heavy_per_minute)
        if wait:
            seconds = int(wait) + 1
            response = _error(path, 429, f"Too many requests. Try again in {seconds} s.")
            response.headers["Retry-After"] = str(seconds)
            return response
        return await call_next(request)
