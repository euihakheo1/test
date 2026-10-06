"""Browser session cookies, CSRF double-submit check and Origin check.

The web app talks to the API same-origin (Next.js rewrites ``/api`` to the backend), so the
browser keeps the tokens in cookies it cannot read from JavaScript:

========== =========== ======== ================= ======================================
cookie     HttpOnly    SameSite Path              purpose
========== =========== ======== ================= ======================================
jt_access  yes         Lax      /api              access JWT (short TTL)
jt_refresh yes         Strict   /api/v1/auth      refresh token (rotated on every use)
jt_csrf    no          Lax      /                 CSRF token the page echoes in a header
========== =========== ======== ================= ======================================

``Secure`` is set in prod (``Settings.secure_cookies``). Cookies carry no ``Domain``
attribute, so they are host-only. Response bodies of cookie sessions never contain tokens.

CSRF: an unsafe request (POST/PUT/PATCH/DELETE) authenticated by cookie must send
``X-CSRF-Token`` equal to the ``jt_csrf`` cookie, and the token must be the HMAC bound to the
same session (see ``AuthService.csrf_token``); otherwise 403 ``csrf_failed``. Requests with an
``Authorization: Bearer`` header are exempt: a cross-site page cannot attach that header
without a CORS preflight that the API refuses for unknown origins.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from fastapi import Request, Response

from jettae.api.errors import ApiError

if TYPE_CHECKING:
    from jettae.api.auth import AuthService, TokenPair
    from jettae.config import Settings

ACCESS_COOKIE = "jt_access"
REFRESH_COOKIE = "jt_refresh"
CSRF_COOKIE = "jt_csrf"
CSRF_HEADER = "X-CSRF-Token"
ACCESS_PATH = "/api"
REFRESH_PATH = "/api/v1/auth"
CSRF_PATH = "/"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def set_session_cookies(
    response: Response, settings: Settings, pair: TokenPair, csrf: str | None
) -> None:
    """Set the access and refresh cookies (and the CSRF cookie when ``csrf`` is given).
    A pair without a refresh token (refresh-reuse grace) leaves the refresh cookie alone: the
    browser already holds the successor set by the overlapping request.

    The CSRF cookie lives as long as the session; it is only replaced at login/signup, so
    requests already in flight with the previous header value keep matching after a refresh.
    """
    secure = settings.secure_cookies
    response.set_cookie(
        ACCESS_COOKIE,
        pair.access_token,
        max_age=pair.expires_in,
        path=ACCESS_PATH,
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    if pair.refresh_token:
        response.set_cookie(
            REFRESH_COOKIE,
            pair.refresh_token,
            max_age=pair.refresh_expires_in or settings.refresh_token_ttl_s,
            path=REFRESH_PATH,
            secure=secure,
            httponly=True,
            samesite="strict",
        )
    if csrf is not None:
        response.set_cookie(
            CSRF_COOKIE,
            csrf,
            max_age=settings.session_max_age_s,
            path=CSRF_PATH,
            secure=secure,
            httponly=False,
            samesite="lax",
        )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    secure = settings.secure_cookies
    for name, path, httponly, samesite in (
        (ACCESS_COOKIE, ACCESS_PATH, True, "lax"),
        (REFRESH_COOKIE, REFRESH_PATH, True, "strict"),
        (CSRF_COOKIE, CSRF_PATH, False, "lax"),
    ):
        response.delete_cookie(
            name,
            path=path,
            secure=secure,
            httponly=httponly,
            samesite=samesite,  # type: ignore[arg-type]
        )


def csrf_failed(message: str = "missing or invalid CSRF token") -> ApiError:
    return ApiError(403, "csrf_failed", message)


def require_csrf(request: Request, auth: AuthService, session_id: str | None) -> None:
    """Double-submit check for a cookie-authenticated unsafe request."""
    header = request.headers.get(CSRF_HEADER)
    cookie = request.cookies.get(CSRF_COOKIE)
    if not header or not cookie:
        raise csrf_failed()
    if not hmac.compare_digest(header.encode("utf-8"), cookie.encode("utf-8")):
        raise csrf_failed()
    if not auth.csrf_valid(session_id, header):
        raise csrf_failed()


def origin_allowed(request: Request, allowed: tuple[str, ...]) -> bool:
    """For unsafe requests: a browser ``Origin`` header must be one of ``allowed``. Requests
    without ``Origin`` (CLI, MCP, server-side calls) pass; so does every request when no
    origins are configured (local development)."""
    if request.method in SAFE_METHODS or not allowed:
        return True
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return origin.rstrip("/") in allowed
