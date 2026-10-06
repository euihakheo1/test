"""Authentication: tenants, users (argon2), memberships/roles, browser sessions, JWT access
tokens (short TTL), rotating refresh tokens (hashed, reuse detection) and API tokens (hashed)
for MCP / CLI clients.

A login creates a session (``auth_sessions`` row, id = refresh-token family = ``sid`` claim).
Every access token is checked against its session on each request, so logout or a detected
refresh-token reuse ends the session at once. The browser receives these tokens only as
HttpOnly cookies (``jettae.api.security``); the service layer here is transport-neutral.

The tenant of every request is derived from the credential only; nothing the client sends
in a body, query or header can select another tenant.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import jwt
import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy.exc import IntegrityError

from jettae.api.errors import ApiError
from jettae.api.ratelimit import FailureLimiter
from jettae.db.config import Settings
from jettae.db.orm import ApiTokenRow, MembershipRow, RefreshTokenRow, TenantRow, UserRow
from jettae.db.orm_auth import AuthSessionRow
from jettae.db.session import Database

ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}
ROLES = tuple(ROLE_RANK)
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")
REFRESH_PREFIX = "jtr_"
API_TOKEN_PREFIX = "jtk_"


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    e = email.strip().lower()
    if not _EMAIL.match(e):
        raise ApiError(422, "invalid_email", "invalid email address")
    return e


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    role: str
    email: str
    method: str  # "cookie" | "jwt" (Bearer access token) | "api_token"
    token_id: str | None = None
    session_id: str | None = None  # browser session (``sid``); None for API tokens

    def has(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    tenant_id: str
    role: str
    user_id: str = ""
    email: str = ""
    session_id: str = ""
    session_expires_at: datetime | None = None
    refresh_expires_in: int = 0
    token_type: str = "bearer"


class AuthService:
    def __init__(self, db: Database, settings: Settings, *, clock: Callable[[], datetime]) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock
        self.secret = settings.resolved_jwt_secret()
        self.hasher = PasswordHasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_kib,
            parallelism=settings.argon2_parallelism,
        )
        self._dummy_hash: str | None = None
        self.limiter: FailureLimiter | None = (
            FailureLimiter(settings.login_max_failures, settings.login_lockout_s, clock)
            if settings.login_max_failures > 0
            else None
        )

    # ------------------------------------------------------------------ passwords
    def _check_password_policy(self, password: str) -> None:
        if len(password) < self.settings.password_min_length:
            raise ApiError(
                422,
                "weak_password",
                f"password must be at least {self.settings.password_min_length} characters",
            )
        if len(password) > 1024:
            raise ApiError(422, "weak_password", "password too long")

    def _verify(self, hashed: str | None, password: str) -> bool:
        if hashed is None:
            # equalise timing for unknown users
            if self._dummy_hash is None:
                self._dummy_hash = self.hasher.hash(secrets.token_urlsafe(16))
            hashed = self._dummy_hash
            with contextlib.suppress(VerifyMismatchError, VerificationError, InvalidHashError):
                self.hasher.verify(hashed, password)
            return False
        try:
            return self.hasher.verify(hashed, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    # ------------------------------------------------------------------ tokens
    def _access_token(self, user_id: str, tenant_id: str, role: str, session_id: str) -> str:
        now = self.clock()
        claims = {
            "sid": session_id,
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
            "sub": user_id,
            "tid": tenant_id,
            "role": role,
            "typ": "access",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self.settings.access_token_ttl_s)).timestamp()),
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(claims, self.secret, algorithm="HS256")

    def _issue(
        self,
        s: Any,
        user_id: str,
        tenant_id: str,
        role: str,
        session: AuthSessionRow | None = None,
    ) -> TokenPair:
        """Issue an access + refresh token. Without ``session`` a new session (family) starts;
        with it, the refresh token stays in that family and never outlives the session."""
        now = self.clock()
        if session is None:
            session = AuthSessionRow(
                id=f"ses_{uuid.uuid4().hex}",
                tenant_id=tenant_id,
                user_id=user_id,
                expires_at=now + timedelta(seconds=self.settings.session_max_age_s),
            )
            s.add(session)
            s.flush()
        refresh_exp = min(
            now + timedelta(seconds=self.settings.refresh_token_ttl_s), session.expires_at
        )
        raw = REFRESH_PREFIX + secrets.token_urlsafe(32)
        s.add(
            RefreshTokenRow(
                id=f"rt_{uuid.uuid4().hex}",
                tenant_id=tenant_id,
                user_id=user_id,
                family_id=session.id,
                token_hash=sha256_hex(raw),
                expires_at=refresh_exp,
            )
        )
        s.flush()
        user = s.get(UserRow, user_id)
        return TokenPair(
            access_token=self._access_token(user_id, tenant_id, role, session.id),
            refresh_token=raw,
            expires_in=self.settings.access_token_ttl_s,
            tenant_id=tenant_id,
            role=role,
            user_id=user_id,
            email=user.email if user is not None else "",
            session_id=session.id,
            session_expires_at=session.expires_at,
            refresh_expires_in=max(0, int((refresh_exp - now).total_seconds())),
        )

    def _revoke_family(self, s: Any, family_id: str, reason: str) -> None:
        now = self.clock()
        s.execute(
            sa.update(RefreshTokenRow)
            .where(RefreshTokenRow.family_id == family_id, RefreshTokenRow.revoked_at.is_(None))
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
        s.execute(
            sa.update(AuthSessionRow)
            .where(AuthSessionRow.id == family_id, AuthSessionRow.revoked_at.is_(None))
            .values(revoked_at=now, revoked_reason=reason)
            .execution_options(synchronize_session=False)
        )

    # ------------------------------------------------------------------ CSRF
    def csrf_token(self, session_id: str) -> str:
        """Double-submit token bound to the session: ``nonce.mac``. Binding to the session
        (HMAC with the server secret) means a cookie planted by a sibling subdomain or left
        from an earlier session does not validate against the current one."""
        nonce = secrets.token_urlsafe(18)
        return f"{nonce}.{self._csrf_mac(session_id, nonce)}"

    def csrf_valid(self, session_id: str | None, token: str | None) -> bool:
        if not session_id or not token or token.count(".") != 1 or len(token) > 256:
            return False
        nonce, mac = token.split(".", 1)
        return hmac.compare_digest(mac, self._csrf_mac(session_id, nonce))

    def _csrf_mac(self, session_id: str, nonce: str) -> str:
        msg = f"jettae-csrf|{session_id}|{nonce}".encode()
        return hmac.new(self.secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()

    # ------------------------------------------------------------------ use cases
    def signup(self, email: str, password: str, tenant_name: str) -> TokenPair:
        e = normalize_email(email)
        self._check_password_policy(password)
        name = tenant_name.strip()
        if not name or len(name) > 200:
            raise ApiError(422, "invalid_tenant_name", "tenant name must be 1-200 characters")
        pw_hash = self.hasher.hash(password)
        tid, uid = f"tn_{uuid.uuid4().hex}", f"usr_{uuid.uuid4().hex}"
        try:
            with self.db.write(tid) as s:
                s.add(TenantRow(id=tid, name=name))
                s.add(UserRow(id=uid, email=e, password_hash=pw_hash, is_active=True))
                s.flush()
                s.add(
                    MembershipRow(
                        id=f"mem_{uuid.uuid4().hex}", tenant_id=tid, user_id=uid, role="owner"
                    )
                )
                s.flush()
                return self._issue(s, uid, tid, "owner")
        except IntegrityError:
            raise ApiError(
                409, "email_taken", "an account with this email already exists"
            ) from None

    def login(self, email: str, password: str, tenant_id: str | None = None) -> TokenPair:
        """Password check in a *read* transaction: argon2 (64 MiB, t=3) must not run while
        the SQLite database-wide write lock (BEGIN IMMEDIATE) is held. The short write
        transaction afterwards only rehashes (if needed) and issues tokens."""
        try:
            e = normalize_email(email)
        except ApiError:
            e = ""
        if self.limiter is not None:
            self.limiter.check(f"login:{e}")
        with self.db.session() as s:
            user = s.scalars(sa.select(UserRow).where(UserRow.email == e)).first() if e else None
            ok = self._verify(user.password_hash if user else None, password)
            if user is None or not ok or not user.is_active:
                if self.limiter is not None:
                    self.limiter.fail(f"login:{e}")
                raise ApiError(401, "invalid_credentials", "invalid email or password")
            user_id, old_hash = user.id, user.password_hash
            mems = [
                (m.tenant_id, m.role)
                for m in s.scalars(
                    sa.select(MembershipRow)
                    .where(MembershipRow.user_id == user.id)
                    .order_by(MembershipRow.created_at, MembershipRow.id)
                )
            ]
        if self.limiter is not None:
            self.limiter.succeed(f"login:{e}")
        if tenant_id is not None:
            mems = [m for m in mems if m[0] == tenant_id]
        if not mems:
            raise ApiError(403, "no_membership", "no access to the requested tenant")
        if len(mems) > 1:
            raise ApiError(
                409,
                "tenant_selection_required",
                "the account belongs to several tenants; pass tenant_id",
                {"tenant_ids": [m[0] for m in mems]},
            )
        new_hash = self.hasher.hash(password) if self.hasher.check_needs_rehash(old_hash) else None
        tid, role = mems[0]
        with self.db.write() as s:
            if new_hash is not None:
                s.execute(
                    sa.update(UserRow)
                    .where(UserRow.id == user_id, UserRow.password_hash == old_hash)
                    .values(password_hash=new_hash)
                )
            return self._issue(s, user_id, tid, role)

    def refresh_session_id(self, refresh_token: str) -> str | None:
        """The session (family) a refresh token belongs to, without consuming it. Used to
        verify the CSRF token before rotation; revoked tokens still resolve, so a replayed
        token with a valid CSRF header reaches reuse detection."""
        with self.db.session() as s:
            row = s.scalars(
                sa.select(RefreshTokenRow).where(
                    RefreshTokenRow.token_hash == sha256_hex(refresh_token)
                )
            ).first()
            return row.family_id if row is not None else None

    def refresh(self, refresh_token: str) -> TokenPair:
        now = self.clock()
        h = sha256_hex(refresh_token)
        reused = False
        with self.db.write() as s:
            row = s.scalars(
                sa.select(RefreshTokenRow).where(RefreshTokenRow.token_hash == h)
            ).first()
            if row is None:
                raise ApiError(401, "invalid_refresh_token", "invalid refresh token")
            if row.revoked_at is not None:
                grace = self._grace_access(s, row, now)
                if grace is not None:
                    return grace
                # Reuse of a rotated (or logged-out) token: assume theft and end the whole
                # session, including access tokens already issued in it (committed).
                self._revoke_family(s, row.family_id, "refresh_reuse")
                reused = True
        if reused:
            raise ApiError(401, "refresh_token_reused", "refresh token already used")
        lost_race = False
        family_id = ""
        with self.db.write() as s:
            row = s.scalars(
                sa.select(RefreshTokenRow).where(RefreshTokenRow.token_hash == h)
            ).first()
            if row is None or row.revoked_at is not None:
                raise ApiError(401, "invalid_refresh_token", "invalid refresh token")
            if row.expires_at <= now:
                raise ApiError(401, "refresh_token_expired", "refresh token expired")
            session = s.get(AuthSessionRow, row.family_id)
            if session is None or session.revoked_at is not None:
                # no session row = issued before revision 0005_auth_sessions: sign in again
                raise ApiError(401, "invalid_refresh_token", "session ended")
            if session.expires_at <= now:
                raise ApiError(401, "session_expired", "session expired; sign in again")
            m = self._membership(s, row.user_id, row.tenant_id)
            if m is None:
                raise ApiError(401, "invalid_refresh_token", "membership no longer exists")
            user_id, tid, role, family_id = row.user_id, row.tenant_id, m.role, row.family_id
            # Atomic consume: on PostgreSQL a concurrent rotation of the same token blocks on
            # the row lock and then sees revoked_at set (rowcount 0) -> treated as reuse.
            res = s.execute(
                sa.update(RefreshTokenRow)
                .where(RefreshTokenRow.token_hash == h, RefreshTokenRow.revoked_at.is_(None))
                .values(revoked_at=now)
                .execution_options(synchronize_session=False)
            )
            if getattr(res, "rowcount", -1) != 1:
                lost_race = True
            else:
                pair = self._issue(s, user_id, tid, role, session)
                s.execute(
                    sa.update(RefreshTokenRow)
                    .where(RefreshTokenRow.token_hash == h)
                    .values(replaced_by=sha256_hex(pair.refresh_token)[:16])
                    .execution_options(synchronize_session=False)
                )
                return pair
        if lost_race:
            with self.db.write() as s:
                # the concurrent rotation that won has committed by now (row lock released)
                lost = s.scalars(
                    sa.select(RefreshTokenRow).where(RefreshTokenRow.token_hash == h)
                ).first()
                grace = self._grace_access(s, lost, now) if lost is not None else None
                if grace is not None:
                    return grace
                self._revoke_family(s, family_id, "refresh_reuse")
        raise ApiError(401, "refresh_token_reused", "refresh token already used")

    def _grace_access(self, s: Any, row: RefreshTokenRow, now: datetime) -> TokenPair | None:
        """A new access token (no refresh token) for a refresh token rotated less than
        ``refresh_reuse_grace_s`` ago in a session that is still valid, else None.

        Why: browser tabs share one cookie jar, so when their common access cookie expires
        they can send the same refresh cookie before the first response's Set-Cookie lands.
        Treating the second as theft would sign the user out everywhere. The successor
        refresh token is not returned (only its hash is stored) and not re-issued, so the
        family stays linear. Reuse after the window, or of a token revoked by logout or
        reuse detection (no successor, or the session already ended), still ends the
        session. The request already passed the session-bound CSRF check."""
        grace = self.settings.refresh_reuse_grace_s
        if grace <= 0 or row.revoked_at is None or row.replaced_by is None:
            return None
        if now - row.revoked_at > timedelta(seconds=grace):
            return None
        session = s.get(AuthSessionRow, row.family_id)
        if session is None or session.revoked_at is not None or session.expires_at <= now:
            return None
        m = self._membership(s, row.user_id, row.tenant_id)
        if m is None:
            return None
        user = s.get(UserRow, row.user_id)
        return TokenPair(
            access_token=self._access_token(row.user_id, row.tenant_id, m.role, session.id),
            refresh_token="",
            expires_in=self.settings.access_token_ttl_s,
            tenant_id=row.tenant_id,
            role=m.role,
            user_id=row.user_id,
            email=user.email if user is not None else "",
            session_id=session.id,
            session_expires_at=session.expires_at,
        )

    def logout(self, refresh_token: str) -> None:
        """End the session the refresh token belongs to (all its refresh and access tokens)."""
        sid = self.refresh_session_id(refresh_token)
        if sid is not None:
            self.end_session(sid)

    def end_session(self, session_id: str, reason: str = "logout") -> None:
        with self.db.write() as s:
            self._revoke_family(s, session_id, reason)

    @staticmethod
    def _membership(s: Any, user_id: str, tenant_id: str) -> MembershipRow | None:
        return s.scalars(
            sa.select(MembershipRow)
            .join(UserRow, UserRow.id == MembershipRow.user_id)
            .where(
                MembershipRow.user_id == user_id,
                MembershipRow.tenant_id == tenant_id,
                UserRow.is_active.is_(True),
            )
        ).first()

    # ------------------------------------------------------------------ request auth
    def authenticate(self, token: str) -> Principal:
        """Bearer credential: API token (``jtk_``) or access JWT."""
        if token.startswith(API_TOKEN_PREFIX):
            return self._auth_api_token(token)
        return self._auth_jwt(token)

    def authenticate_cookie(self, token: str) -> Principal:
        """Access-token cookie. API tokens are never accepted from a cookie: they are not
        bound to a session, so a CSRF token could not be tied to anything."""
        if token.startswith(API_TOKEN_PREFIX):
            raise ApiError(401, "invalid_token", "invalid access token")
        p = self._auth_jwt(token)
        return Principal(
            p.user_id, p.tenant_id, p.role, p.email, "cookie", p.token_id, p.session_id
        )

    def _auth_jwt(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self.secret,
                algorithms=["HS256"],
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
                options={
                    "require": ["exp", "iat", "sub", "tid", "typ", "sid"],
                    "verify_exp": False,  # checked below against the injectable clock
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError:
            raise ApiError(401, "invalid_token", "invalid access token") from None
        if claims.get("typ") != "access":
            raise ApiError(401, "invalid_token", "invalid access token")
        if int(claims["exp"]) <= int(self.clock().timestamp()):
            raise ApiError(401, "token_expired", "access token expired")
        now = self.clock()
        with self.db.session() as s:
            session = s.get(AuthSessionRow, str(claims["sid"]))
            if (
                session is None
                or session.revoked_at is not None
                or session.expires_at <= now
                or session.user_id != claims["sub"]
                or session.tenant_id != claims["tid"]
            ):
                raise ApiError(401, "session_ended", "session ended; sign in again")
            m = self._membership(s, claims["sub"], claims["tid"])
            if m is None:
                raise ApiError(401, "invalid_token", "membership no longer exists")
            user = s.get(UserRow, claims["sub"])
            email = user.email if user else ""
            return Principal(
                claims["sub"], claims["tid"], m.role, email, "jwt", claims.get("jti"), session.id
            )

    def _auth_api_token(self, token: str) -> Principal:
        now = self.clock()
        with self.db.session() as s:
            row = s.scalars(
                sa.select(ApiTokenRow).where(ApiTokenRow.token_hash == sha256_hex(token))
            ).first()
            if row is None or row.revoked_at is not None:
                raise ApiError(401, "invalid_token", "invalid API token")
            if row.expires_at is not None and row.expires_at <= now:
                raise ApiError(401, "token_expired", "API token expired")
            m = self._membership(s, row.user_id, row.tenant_id)
            if m is None:
                raise ApiError(401, "invalid_token", "membership no longer exists")
            role = min(row.role, m.role, key=ROLE_RANK.__getitem__)
            user = s.get(UserRow, row.user_id)
            email = user.email if user else ""
            principal = Principal(row.user_id, row.tenant_id, role, email, "api_token", row.id)
            stale = row.last_used_at is None or (now - row.last_used_at) > timedelta(minutes=1)
        if stale:
            with self.db.write() as s:
                s.execute(
                    sa.update(ApiTokenRow).where(ApiTokenRow.id == row.id).values(last_used_at=now)
                )
        return principal

    # ------------------------------------------------------------------ API tokens
    def create_api_token(
        self, p: Principal, name: str, role: str, expires_in_days: int | None
    ) -> tuple[dict[str, Any], str]:
        if role not in ROLE_RANK:
            raise ApiError(422, "invalid_role", f"role must be one of {', '.join(ROLES)}")
        if ROLE_RANK[role] > ROLE_RANK[p.role]:
            raise ApiError(403, "forbidden", "cannot create a token with more rights than yours")
        now = self.clock()
        raw = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
        row = ApiTokenRow(
            id=f"tok_{uuid.uuid4().hex}",
            tenant_id=p.tenant_id,
            user_id=p.user_id,
            name=name.strip()[:200] or "token",
            token_hash=sha256_hex(raw),
            role=role,
            expires_at=(now + timedelta(days=expires_in_days)) if expires_in_days else None,
        )
        with self.db.write(p.tenant_id) as s:
            s.add(row)
            s.flush()
            return _token_view(row), raw

    def list_api_tokens(self, p: Principal) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.scalars(
                sa.select(ApiTokenRow)
                .where(ApiTokenRow.tenant_id == p.tenant_id)
                .order_by(ApiTokenRow.created_at, ApiTokenRow.id)
            )
            return [_token_view(r) for r in rows]

    def revoke_api_token(self, p: Principal, token_id: str) -> None:
        with self.db.write(p.tenant_id) as s:
            row = s.get(ApiTokenRow, token_id)
            if row is None or row.tenant_id != p.tenant_id:
                raise ApiError(404, "not_found", "resource not found")
            if row.revoked_at is None:
                row.revoked_at = self.clock()

    def tenant_name(self, tenant_id: str) -> str | None:
        with self.db.session() as s:
            t = s.get(TenantRow, tenant_id)
            return t.name if t else None


def _token_view(r: ApiTokenRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "role": r.role,
        "created_at": r.created_at,
        "expires_at": r.expires_at,
        "revoked_at": r.revoked_at,
        "last_used_at": r.last_used_at,
    }
