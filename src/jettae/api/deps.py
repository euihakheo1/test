"""FastAPI dependencies: runtime, authenticated principal, roles, cursors, idempotency."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import IntegrityError

from jettae.api.auth import AuthService, Principal
from jettae.api.errors import ApiError
from jettae.api.security import ACCESS_COOKIE, SAFE_METHODS, require_csrf
from jettae.db.orm import IdempotencyKeyRow
from jettae.db.runtime import Runtime

_bearer = HTTPBearer(auto_error=False, description="JWT access token or API token (jtk_...)")


def get_runtime(request: Request) -> Runtime:
    rt: Runtime = request.app.state.runtime
    return rt


def get_auth(request: Request) -> AuthService:
    auth: AuthService = request.app.state.auth
    return auth


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
AuthDep = Annotated[AuthService, Depends(get_auth)]


def get_principal(
    request: Request,
    auth: AuthDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """``Authorization: Bearer`` (API token or access JWT; no CSRF check) takes precedence;
    otherwise the ``jt_access`` cookie, with the CSRF check on unsafe methods. A request
    that sends an invalid Bearer header is rejected even if it also carries a valid cookie,
    so a client never silently runs under a different identity than the one it sent."""
    if creds is not None and creds.scheme.lower() == "bearer" and creds.credentials:
        p = auth.authenticate(creds.credentials)
    else:
        cookie = request.cookies.get(ACCESS_COOKIE)
        if not cookie:
            raise ApiError(
                401,
                "unauthorized",
                "authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        p = auth.authenticate_cookie(cookie)
        if request.method not in SAFE_METHODS:
            require_csrf(request, auth, p.session_id)
    request.state.principal = p
    return p


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require(role: str) -> Callable[[Principal], Principal]:
    def dep(p: PrincipalDep) -> Principal:
        if not p.has(role):
            raise ApiError(403, "forbidden", f"requires role '{role}' or higher")
        return p

    return dep


Viewer = Annotated[Principal, Depends(require("viewer"))]
Member = Annotated[Principal, Depends(require("member"))]
Admin = Annotated[Principal, Depends(require("admin"))]


# --------------------------------------------------------------------------- cursors
def encode_cursor(value: Any) -> str:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> Any:
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        return json.loads(base64.urlsafe_b64decode(cursor + pad))
    except (ValueError, json.JSONDecodeError):
        raise ApiError(400, "invalid_cursor", "invalid pagination cursor") from None


def clamp_limit(limit: int | None, default: int = 50, maximum: int = 200) -> int:
    if limit is None:
        return default
    if limit < 1 or limit > maximum:
        raise ApiError(422, "invalid_limit", f"limit must be between 1 and {maximum}")
    return limit


# --------------------------------------------------------------------------- idempotency
IdemKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="Retries with the same key and body return the first response.",
        max_length=200,
    ),
]

STALE_PENDING = timedelta(minutes=5)


def fingerprint(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, bytes):
            h.update(b"b:" + hashlib.sha256(p).digest())
        else:
            h.update(b"j:" + json.dumps(p, sort_keys=True, default=str).encode())
    return h.hexdigest()


def idempotent(
    rt: Runtime,
    principal: Principal,
    key: str | None,
    endpoint: str,
    request_hash: str,
    fn: Callable[[], tuple[int, Any]],
    headers_for: Callable[[Any], dict[str, str]] | None = None,
) -> JSONResponse:
    """Run ``fn`` at most once per (tenant, endpoint, key). Same key + same request -> the
    stored response is replayed; same key + different request -> 422; a concurrent request
    with the key still in flight -> 409."""
    if key is None:
        status, body = fn()
        return JSONResponse(body, status_code=status, headers=_h(headers_for, body))
    if not key.strip() or not key.isprintable():
        raise ApiError(422, "invalid_idempotency_key", "Idempotency-Key must be printable text")
    tenant = principal.tenant_id
    now = rt.clock()
    pk = (tenant, endpoint, key)
    try:
        with rt.db.write(tenant) as s:
            row = s.get(IdempotencyKeyRow, pk)
            if row is not None and row.state == "pending" and now - row.created_at > STALE_PENDING:
                s.delete(row)  # abandoned by a crashed request
                s.flush()
                row = None
            if row is None:
                s.add(
                    IdempotencyKeyRow(
                        tenant_id=tenant,
                        endpoint=endpoint,
                        key=key,
                        request_hash=request_hash,
                        state="pending",
                        created_at=now,
                        updated_at=now,
                    )
                )
                s.flush()
                existing = None
            else:
                existing = (row.request_hash, row.state, row.response_status, row.response_body)
    except IntegrityError:
        raise ApiError(
            409, "idempotency_in_progress", "a request with this Idempotency-Key is in progress"
        ) from None
    if existing is not None:
        req_hash, state, st, body = existing
        if req_hash != request_hash:
            raise ApiError(
                422,
                "idempotency_key_reused",
                "Idempotency-Key was already used with a different request",
            )
        if state != "done":
            raise ApiError(
                409, "idempotency_in_progress", "a request with this Idempotency-Key is in progress"
            )
        return JSONResponse(
            body,
            status_code=st or 200,
            headers={**_h(headers_for, body), "Idempotent-Replayed": "true"},
        )
    try:
        status, body = fn()
    except BaseException:
        with rt.db.write(tenant) as s:
            s.execute(
                sa.delete(IdempotencyKeyRow).where(
                    IdempotencyKeyRow.tenant_id == tenant,
                    IdempotencyKeyRow.endpoint == endpoint,
                    IdempotencyKeyRow.key == key,
                )
            )
        raise
    with rt.db.write(tenant) as s:
        row = s.get(IdempotencyKeyRow, pk)
        if row is not None:
            row.state, row.response_status, row.response_body = "done", status, body
            row.updated_at = rt.clock()
    return JSONResponse(body, status_code=status, headers=_h(headers_for, body))


def _h(headers_for: Callable[[Any], dict[str, str]] | None, body: Any) -> dict[str, str]:
    return headers_for(body) if headers_for is not None else {}


def get_ops(request: Request) -> Any:
    return request.app.state.ops
