"""Loopback API security: host allowlist, origin checks, session capability, CSP.

Binding to ``127.0.0.1`` is not authentication. A malicious page in the user's
browser can still send requests to a loopback server, and DNS rebinding can point
an attacker-controlled hostname at 127.0.0.1. This module therefore enforces:

1. **Host allowlist** - only ``127.0.0.1:<port>`` / ``localhost:<port>`` /
   ``[::1]:<port>`` are accepted (defeats DNS rebinding).
2. **Origin allowlist** - a present ``Origin`` must match the served origin.
3. **Session capability** - every state-changing request must carry a random
   per-run token in a header (defeats CSRF and blind cross-site requests). The
   token is delivered in a response body to same-origin callers only; it is never
   placed in a URL, query string or log line.
4. **Path discipline** - traversal segments and absolute-form targets are rejected
   before routing.
5. **Response hardening** - CSP without any remote origin, no framing, no sniffing.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

CAPABILITY_HEADER = "X-PhuThanhAI-Capability"
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

logger = logging.getLogger("phuthanhai.security")


@dataclass(frozen=True)
class SecurityPolicy:
    """Immutable per-run security configuration."""

    port: int
    capability: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    allowed_hosts: frozenset[str] = frozenset()
    allowed_origins: frozenset[str] = frozenset()

    @classmethod
    def for_loopback(cls, port: int) -> "SecurityPolicy":
        hosts = frozenset(
            {
                f"127.0.0.1:{port}",
                f"localhost:{port}",
                f"[::1]:{port}",
            }
        )
        origins = frozenset(
            {
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
                f"http://[::1]:{port}",
            }
        )
        return cls(port=port, allowed_hosts=hosts, allowed_origins=origins)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _deny(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "user_message": message, "retryable": False},
        headers={"Cache-Control": "no-store"},
    )


def _has_traversal(path: str) -> bool:
    if ".." in path.split("/"):
        return True
    if "\\" in path:
        return True
    if "//" in path:
        return True
    return False


class SecurityMiddleware(BaseHTTPMiddleware):
    """Reject untrusted Host/Origin/target and enforce the session capability."""

    def __init__(self, app, policy: SecurityPolicy) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self.policy = policy

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        raw_target = request.scope.get("raw_path", b"").decode("latin-1") or path
        if _has_traversal(path) or _has_traversal(raw_target) or raw_target.startswith("http"):
            logger.warning("rejected suspicious request target")
            return _deny(400, "SECURITY_DENIED", "Yeu cau khong hop le.")

        host = (request.headers.get("host") or "").strip().lower()
        if host not in self.policy.allowed_hosts:
            logger.warning("rejected host header")
            return _deny(400, "SECURITY_DENIED", "Host khong duoc phep.")

        origin = request.headers.get("origin")
        if origin is not None and origin not in self.policy.allowed_origins:
            logger.warning("rejected origin")
            return _deny(403, "SECURITY_DENIED", "Origin khong duoc phep.")

        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site == "cross-site":
            logger.warning("rejected cross-site fetch")
            return _deny(403, "SECURITY_DENIED", "Yeu cau cross-site bi tu choi.")

        if request.method in STATE_CHANGING_METHODS:
            supplied = request.headers.get(CAPABILITY_HEADER, "")
            if not supplied or not secrets.compare_digest(supplied, self.policy.capability):
                return _deny(403, "SECURITY_DENIED", "Thieu hoac sai ma phien lam viec.")

        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
