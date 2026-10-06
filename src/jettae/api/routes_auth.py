"""/auth: signup, login, refresh, logout, me, API tokens.

Browser sessions use HttpOnly cookies only (``jettae.api.security``); no endpoint returns an
access or refresh token in a body. CLI and MCP clients use API tokens (``jtk_``) created
here and sent as ``Authorization: Bearer``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from jettae.api.auth import AuthService, TokenPair
from jettae.api.deps import Admin, AuthDep, PrincipalDep, Viewer
from jettae.api.errors import ERROR_RESPONSES, ApiError
from jettae.api.schemas import (
    ApiTokenCreate,
    ApiTokenCreated,
    ApiTokenOut,
    LoginRequest,
    MeResponse,
    SessionResponse,
    SessionTenant,
    SessionUser,
    SignupRequest,
)
from jettae.api.security import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    clear_session_cookies,
    require_csrf,
    set_session_cookies,
)

router = APIRouter(prefix="/auth", tags=["auth"], responses=ERROR_RESPONSES)


def ip_throttle(request: Request) -> None:
    """Per-client-IP request limit for unauthenticated auth endpoints (429 + Retry-After)."""
    limiter = getattr(request.app.state, "auth_ip_limiter", None)
    if limiter is not None:
        host = request.client.host if request.client else "unknown"
        limiter.hit(host)


Throttled = [Depends(ip_throttle)]


def _session_body(pair: TokenPair, auth: AuthService) -> SessionResponse:
    return SessionResponse(
        user=SessionUser(id=pair.user_id, email=pair.email, role=pair.role),
        tenant=SessionTenant(id=pair.tenant_id, name=auth.tenant_name(pair.tenant_id)),
        tenant_id=pair.tenant_id,
        role=pair.role,
        expires_in=pair.expires_in,
        session_expires_at=pair.session_expires_at,
    )


def _start_session(pair: TokenPair, auth: AuthService, response: Response) -> SessionResponse:
    set_session_cookies(response, auth.settings, pair, auth.csrf_token(pair.session_id))
    return _session_body(pair, auth)


@router.post("/signup", status_code=201, response_model=SessionResponse, dependencies=Throttled)
def signup(body: SignupRequest, auth: AuthDep, response: Response) -> SessionResponse:
    """새 회사(테넌트)와 소유자 계정을 만들고 로그인 쿠키를 설정한다."""
    return _start_session(auth.signup(body.email, body.password, body.tenant_name), auth, response)


@router.post("/login", response_model=SessionResponse, dependencies=Throttled)
def login(body: LoginRequest, auth: AuthDep, response: Response) -> SessionResponse:
    """Sets ``jt_access`` / ``jt_refresh`` (HttpOnly) and ``jt_csrf`` cookies."""
    return _start_session(auth.login(body.email, body.password, body.tenant_id), auth, response)


@router.post("/refresh", response_model=SessionResponse, dependencies=Throttled)
def refresh(request: Request, auth: AuthDep, response: Response) -> SessionResponse:
    """Rotate the refresh cookie. Requires ``X-CSRF-Token``. Presenting an already rotated
    refresh token ends the whole session (reuse is treated as theft)."""
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise ApiError(401, "invalid_refresh_token", "no refresh cookie")
    sid = auth.refresh_session_id(token)
    if sid is None:
        raise ApiError(401, "invalid_refresh_token", "invalid refresh token")
    require_csrf(request, auth, sid)
    pair = auth.refresh(token)
    set_session_cookies(response, auth.settings, pair, None)
    return _session_body(pair, auth)


@router.post("/logout", status_code=204)
def logout(request: Request, auth: AuthDep) -> Response:
    """End the session (its refresh tokens and access tokens) and clear all three cookies.
    Requires ``X-CSRF-Token`` when the cookies name a known session; without one there is
    nothing to revoke and the cookies are only cleared."""
    sid: str | None = None
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        sid = auth.refresh_session_id(token)
    if sid is None and request.cookies.get(ACCESS_COOKIE):
        try:
            sid = auth.authenticate_cookie(request.cookies[ACCESS_COOKIE]).session_id
        except ApiError:
            sid = None
    if sid is not None:
        require_csrf(request, auth, sid)
        auth.end_session(sid)
    out = Response(status_code=204)
    clear_session_cookies(out, auth.settings)
    return out


@router.get("/me", response_model=MeResponse)
def me(p: PrincipalDep, auth: AuthDep) -> MeResponse:
    name = auth.tenant_name(p.tenant_id)
    return MeResponse(
        user=SessionUser(id=p.user_id, email=p.email, role=p.role),
        tenant=SessionTenant(id=p.tenant_id, name=name),
        user_id=p.user_id,
        email=p.email,
        tenant_id=p.tenant_id,
        tenant_name=name,
        role=p.role,
        auth_method=p.method,
    )


@router.post("/api-tokens", status_code=201, response_model=ApiTokenCreated)
def create_api_token(body: ApiTokenCreate, p: Admin, auth: AuthDep) -> ApiTokenCreated:
    """MCP·CLI용 API 토큰. 원문은 한 번만 보여 주고 해시만 저장한다."""
    view, raw = auth.create_api_token(p, body.name, body.role, body.expires_in_days)
    return ApiTokenCreated(**view, token=raw)


@router.get("/api-tokens", response_model=list[ApiTokenOut])
def list_api_tokens(p: Viewer, auth: AuthDep) -> list[ApiTokenOut]:
    return [ApiTokenOut(**v) for v in auth.list_api_tokens(p)]


@router.delete("/api-tokens/{token_id}", status_code=204)
def revoke_api_token(token_id: str, p: Admin, auth: AuthDep) -> Response:
    auth.revoke_api_token(p, token_id)
    return Response(status_code=204)
