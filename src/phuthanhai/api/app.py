"""FastAPI application factory for the loopback-only backend.

Lifecycle guarantees:

* exactly one server instance per data root (POSIX advisory lock),
* a per-run capability written to ``runtime/session.json`` with mode ``0600``
  (never to a URL and never logged),
* every spawned child process is terminated and reaped on shutdown,
* the web bundle is served from disk - no CDN, no remote font, no remote script,
* model inference never happens in this process.
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from phuthanhai import PRODUCT_VERSION
from phuthanhai.api.routes import router
from phuthanhai.api.security import SecurityMiddleware, SecurityPolicy
from phuthanhai.config import OFFLINE_ENVIRONMENT, Settings
from phuthanhai.contracts.validators import ContractError
from phuthanhai.library.service import LibraryService
from phuthanhai.library.store import StoreError
from phuthanhai.runtime.scheduler import PROCESSES
from phuthanhai.utils.fs import atomic_write_json
from phuthanhai.utils.logging import configure, get_logger

logger = get_logger("api")


class SingleInstanceError(RuntimeError):
    code = "INSTANCE_ALREADY_RUNNING"


@contextmanager
def instance_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for the lifetime of the server."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    os.chmod(path, 0o600)
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            holder = handle.read().strip() or "unknown pid"
            raise SingleInstanceError(f"another PhuThanhAI backend already runs ({holder})") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def create_app(
    settings: Settings | None = None,
    *,
    port: int = 0,
    capability: str | None = None,
) -> FastAPI:
    settings = settings or Settings.load()
    # Enforce offline defaults for the whole process before any library import.
    os.environ.update(OFFLINE_ENVIRONMENT)
    settings.data.ensure()
    configure(log_file=settings.data.logs / "backend.jsonl")

    policy = SecurityPolicy.for_loopback(port)
    if capability is not None:
        policy = SecurityPolicy(
            port=port,
            capability=capability,
            allowed_hosts=policy.allowed_hosts,
            allowed_origins=policy.allowed_origins,
        )

    library = LibraryService(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.data.ensure()
        with instance_lock(settings.data.instance_lock):
            session_path = settings.data.session_file
            atomic_write_json(
                session_path,
                {
                    "pid": os.getpid(),
                    "port": port,
                    "origin": policy.origin,
                    "capability": policy.capability,
                    "product_version": PRODUCT_VERSION,
                    "started_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                },
                mode=0o600,
            )
            app.state.session_file = session_path
            logger.info("backend started", extra={"fields": {"port": port, "pid": os.getpid()}})
            try:
                yield
            finally:
                library.close()
                terminated = PROCESSES.shutdown()
                try:
                    session_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("could not remove session file")
                logger.info(
                    "backend stopped",
                    extra={"fields": {"terminated_children": terminated}},
                )

    app = FastAPI(
        title="PhuThanhAI",
        version=PRODUCT_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.policy = policy
    app.state.library = library
    app.state.pid = os.getpid()
    app.state.started_at = time.monotonic()

    app.add_middleware(SecurityMiddleware, policy=policy)
    app.include_router(router)

    @app.exception_handler(StoreError)
    async def store_error_handler(_: Request, exc: StoreError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "code": exc.code,
                "user_message": str(exc),
                "retryable": exc.status >= 500,
            },
        )

    @app.exception_handler(ContractError)
    async def contract_error_handler(_: Request, exc: ContractError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": exc.code,
                "user_message": str(exc),
                "retryable": False,
                "next_action": "Kiem tra lai tep du lieu theo contract V2/V3.",
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
        # Full traceback stays in the local log; the client gets a stable code.
        logger.exception("unhandled API error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "user_message": "Backend gap loi khong mong doi. Xem log cuc bo.",
                "retryable": True,
            },
        )

    if (settings.web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(settings.web_dist), html=True), name="web")
    else:
        logger.warning("web bundle missing; API only", extra={"fields": {"expected": str(settings.web_dist)}})

        @app.get("/")
        def missing_bundle() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "code": "WEB_BUNDLE_MISSING",
                    "user_message": "Frontend chua duoc build. Chay: cd web && npm install && npm run build",
                    "retryable": True,
                },
            )

    return app
