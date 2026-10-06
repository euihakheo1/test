"""FastAPI application factory (versioned under ``/api/v1``)."""

from __future__ import annotations

import importlib
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import jettae
from jettae.api import (
    routes_auth,
    routes_changes,
    routes_decisions,
    routes_documents,
    routes_health,
    routes_jobs,
    routes_public,
    routes_reports,
)
from jettae.api.auth import AuthService
from jettae.api.errors import install_error_handlers
from jettae.api.ops import API, Ops
from jettae.api.ratelimit import WindowLimiter
from jettae.api.security import CSRF_HEADER, SAFE_METHODS
from jettae.config import Settings, require_valid_environment
from jettae.db.runtime import Runtime

log = logging.getLogger("jettae.api.access")
_RID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Routers owned by other packages, included when their module exists. A module that is
# missing is skipped; a module that exists but fails to import is a bug and is raised.
OPTIONAL_ROUTERS = ["jettae.api.routes_investigations"]


class RequestContextMiddleware:
    """Request id (``X-Request-ID``), security headers and one structured access-log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if _RID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        start = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                headers.append((b"x-request-id", rid.encode()))
                if b"x-content-type-options" not in names:
                    headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"referrer-policy", b"no-referrer"))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            principal = scope.get("state", {}).get("principal")
            log.info(
                "request",
                extra={
                    "request_id": rid,
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status["code"],
                    "duration_ms": int((time.perf_counter() - start) * 1000),
                    "tenant_id": getattr(principal, "tenant_id", None),
                },
            )


class BodySizeLimitMiddleware:
    """Reject bodies over the limit while streaming (before they are spooled to disk).
    multipart/form-data: ``max_upload_bytes`` + 64 KiB form overhead; everything else:
    ``max_json_body_bytes``."""

    def __init__(self, app: ASGIApp, *, max_upload: int, max_other: int) -> None:
        self.app = app
        self.max_upload = max_upload + 64 * 1024
        self.max_other = max_other

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        ctype = headers.get(b"content-type", b"").decode("latin-1").lower()
        limit = self.max_upload if ctype.startswith("multipart/form-data") else self.max_other
        detail = f"request body exceeds {limit} bytes"
        cl = headers.get(b"content-length")
        if cl is not None and cl.isdigit() and int(cl) > limit:
            await _send_error(send, scope, 413, "payload_too_large", detail)
            return
        received = 0

        async def limited() -> Message:
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > limit:
                    raise StarletteHTTPException(413, detail)
            return msg

        await self.app(scope, limited, send)


async def _send_error(send: Send, scope: Scope, status: int, code: str, message: str) -> None:
    import json

    rid = scope.get("state", {}).get("request_id")
    body = json.dumps(
        {"error": {"code": code, "message": message, "details": None, "request_id": rid}}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class OriginCheckMiddleware:
    """Refuse unsafe requests whose browser ``Origin`` is not an allowed origin (403
    ``csrf_failed``). Defence in depth next to the CSRF token: it also covers login and
    signup, which have no session yet. Inactive when no origins are configured (dev)."""

    def __init__(self, app: ASGIApp, *, allowed: tuple[str, ...]) -> None:
        self.app = app
        self.allowed = frozenset(allowed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.allowed and scope.get("method") not in SAFE_METHODS:
            origin = dict(scope.get("headers") or []).get(b"origin")
            if origin is not None and origin.decode("latin-1").rstrip("/") not in self.allowed:
                await _send_error(send, scope, 403, "csrf_failed", "origin not allowed")
                return
        await self.app(scope, receive, send)


def _optional_routers() -> list[Any]:
    out: list[Any] = []
    for name in OPTIONAL_ROUTERS:
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name and (name == e.name or name.startswith(e.name + ".")):
                continue
            raise
        routers = getattr(mod, "routers", None) or [getattr(mod, "router", None)]
        out.extend(r for r in routers if r is not None)
    return out


def create_app(runtime: Runtime | None = None, settings: Settings | None = None) -> FastAPI:
    rt = runtime or Runtime.build(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Startup gate for servers built without app_factory (e.g. embedding create_app);
        # raises SystemExit with a readable message, which aborts server startup.
        require_valid_environment("api", settings=rt.settings, load_dotenv=False)
        yield

    app = FastAPI(
        lifespan=lifespan,
        title="제때받기 API",
        version=getattr(jettae, "__version__", "0.1.0"),
        description=(
            "정산 문서·입금 대사, 지급기한·지연이자 계산, 필요 서류 안내. "
            "결과는 차이·필요 서류·확인이 필요한 조건·관련 근거로만 표현하며 "
            "법적 판단을 하지 않는다. "
            "테넌트는 인증 정보로만 결정된다."
        ),
        openapi_url=f"{API}/openapi.json",
        docs_url=f"{API}/docs",
        redoc_url=None,
    )
    app.state.runtime = rt
    app.state.auth = AuthService(rt.db, rt.settings, clock=rt.clock)
    app.state.auth_ip_limiter = (
        WindowLimiter(rt.settings.auth_ip_per_minute, 60.0, rt.clock)
        if rt.settings.auth_ip_per_minute > 0
        else None
    )
    app.state.refresh_limiter = (
        WindowLimiter(rt.settings.refresh_per_minute, 60.0, rt.clock)
        if rt.settings.refresh_per_minute > 0
        else None
    )
    app.state.public_ip_limiter = (
        WindowLimiter(rt.settings.public_ip_per_minute, 60.0, rt.clock)
        if rt.settings.public_ip_per_minute > 0
        else None
    )
    app.state.ops = Ops(rt)
    install_error_handlers(app)
    for r in (
        routes_health.router,
        routes_auth.router,
        routes_documents.router,
        routes_jobs.router,
        routes_decisions.router,
        routes_decisions.links_router,
        routes_changes.router,
        routes_reports.router,
        routes_public.router,
        *_optional_routers(),
    ):
        app.include_router(r, prefix=API)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_upload=rt.settings.max_upload_bytes,
        max_other=rt.settings.max_json_body_bytes,
    )
    origins = tuple(rt.settings.allowed_origins)
    app.add_middleware(OriginCheckMiddleware, allowed=origins)
    if origins:
        # Exact origins only (wildcards are rejected by Settings), with credentials so the
        # browser may send the session cookies cross-origin when the web app is not proxied.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "X-Request-ID",
                CSRF_HEADER,
            ],
            expose_headers=["X-Request-ID", "Location", "Idempotent-Replayed"],
        )
    app.add_middleware(RequestContextMiddleware)
    return app


def app_factory() -> Any:
    """Entry point for ``uvicorn --factory jettae.api.main:app_factory``: loads ``.env``
    and refuses to build the app when the environment is invalid."""
    return create_app(settings=require_valid_environment("api"))
