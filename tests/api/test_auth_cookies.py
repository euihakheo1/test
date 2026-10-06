"""Browser sessions in HttpOnly cookies: cookie attributes, CSRF double-submit, Bearer
exemption, refresh rotation / reuse, logout, absolute session lifetime, Origin check and
tenant isolation with valid cookies."""

from __future__ import annotations

import json
from http.cookies import SimpleCookie

import jwt
import pytest
from jt_api_helpers import (
    LEDGER_CSV,
    PASSWORD,
    bearer_from_cookies,
    login_session,
    send,
    session_cookies,
    signup,
)

from jettae.config import Settings
from jettae.db import migrate
from jettae.db.runtime import Runtime

API = "/api/v1"
STRONG = "k3Y-" + "Zq9wX2mV7pL4tR8sN1bC6hJ0dF5gA3eU" + "yT"


def _set_cookies(r) -> dict[str, dict[str, str]]:
    """Parse every Set-Cookie header into {name: {attr: value}} (flags as 'true')."""
    out: dict[str, dict[str, str]] = {}
    for raw in r.headers.get_list("set-cookie"):
        c = SimpleCookie()
        c.load(raw)
        for name, morsel in c.items():
            attrs = {k: str(v) for k, v in morsel.items() if v not in ("", False)}
            attrs["value"] = morsel.value
            # SimpleCookie drops flags it does not know; read them from the raw header
            low = raw.lower()
            attrs["httponly"] = "true" if "httponly" in low else ""
            attrs["secure"] = "true" if "; secure" in low else ""
            out[name] = attrs
    return out


def _signup_cookies(client, email="c@c.example", tenant="쿠키상사"):
    r = client.post(
        f"{API}/auth/signup", json={"email": email, "password": PASSWORD, "tenant_name": tenant}
    )
    assert r.status_code == 201, r.text
    client.cookies.clear()
    return r, session_cookies(r)


def _no_tokens_in(body: object, cookies: dict[str, str]) -> None:
    text = json.dumps(body)
    assert "access_token" not in text and "refresh_token" not in text
    for name in ("jt_access", "jt_refresh"):
        assert cookies.get(name, "~absent~") not in text


# ------------------------------------------------------------------ cookie attributes
def test_login_sets_three_cookies_and_no_tokens_in_body(client):
    r, cookies = _signup_cookies(client)
    assert set(cookies) == {"jt_access", "jt_refresh", "jt_csrf"}
    _no_tokens_in(r.json(), cookies)
    sc = _set_cookies(r)
    assert sc["jt_access"]["path"] == "/api" and sc["jt_access"]["httponly"] == "true"
    assert sc["jt_access"]["samesite"].lower() == "lax"
    assert sc["jt_refresh"]["path"] == "/api/v1/auth" and sc["jt_refresh"]["httponly"] == "true"
    assert sc["jt_refresh"]["samesite"].lower() == "strict"
    assert sc["jt_csrf"]["path"] == "/" and sc["jt_csrf"]["httponly"] == ""
    assert sc["jt_csrf"]["samesite"].lower() == "lax"
    # test env: no Secure, so http://localhost works
    assert all(sc[n]["secure"] == "" for n in sc)
    # no Domain attribute: host-only cookies
    assert all("domain" not in sc[n] for n in sc)
    # login, refresh and me bodies carry no token either
    login = client.post(f"{API}/auth/login", json={"email": "c@c.example", "password": PASSWORD})
    client.cookies.clear()
    _no_tokens_in(login.json(), session_cookies(login))
    ref = send(client, "POST", f"{API}/auth/refresh", session_cookies(login))
    assert ref.status_code == 200
    _no_tokens_in(ref.json(), session_cookies(ref))
    me = send(client, "GET", f"{API}/auth/me", session_cookies(login))
    _no_tokens_in(me.json(), session_cookies(login))


@pytest.fixture
def prod_client(tmp_path, clock, ingest, make_client):
    st = Settings(
        env="prod",
        database_url=f"sqlite:///{tmp_path.as_posix()}/p.db",  # app built directly; the
        blob_dir=tmp_path / "pb",  # startup gate (PostgreSQL) is tested in tests/config
        jwt_secret=STRONG,
        allowed_origins=("https://app.example",),
        argon2_time_cost=1,
        argon2_memory_kib=8192,
        argon2_parallelism=1,
    )
    migrate.upgrade(st.database_url)
    r = Runtime.build(st, clock=clock, ingest=ingest)
    yield make_client(r)
    r.close()


def test_prod_cookie_flags(prod_client):
    r = prod_client.post(
        f"{API}/auth/signup",
        json={"email": "p@p.example", "password": PASSWORD, "tenant_name": "P"},
        headers={"Origin": "https://app.example"},
    )
    assert r.status_code == 201, r.text
    sc = _set_cookies(r)
    for name in ("jt_access", "jt_refresh", "jt_csrf"):
        assert sc[name]["secure"] == "true", name
    assert sc["jt_access"]["httponly"] == "true" and sc["jt_refresh"]["httponly"] == "true"
    assert sc["jt_csrf"]["httponly"] == ""
    assert (sc["jt_access"]["path"], sc["jt_refresh"]["path"], sc["jt_csrf"]["path"]) == (
        "/api",
        "/api/v1/auth",
        "/",
    )
    assert sc["jt_refresh"]["samesite"].lower() == "strict"
    # logout clears all three with the same attributes
    prod_client.cookies.clear()
    cookies = session_cookies(r)
    out = send(
        prod_client,
        "POST",
        f"{API}/auth/logout",
        cookies,
        headers={"Origin": "https://app.example"},
    )
    assert out.status_code == 204
    cleared = _set_cookies(out)
    assert set(cleared) == {"jt_access", "jt_refresh", "jt_csrf"}
    for name, attrs in cleared.items():
        assert attrs.get("max-age") == "0" and attrs["secure"] == "true", name


def test_prod_origin_check_and_cors(prod_client):
    body = {"email": "o@o.example", "password": PASSWORD, "tenant_name": "O"}
    evil = prod_client.post(
        f"{API}/auth/signup", json=body, headers={"Origin": "https://evil.example"}
    )
    assert evil.status_code == 403 and evil.json()["error"]["code"] == "csrf_failed"
    ok = prod_client.post(
        f"{API}/auth/signup", json=body, headers={"Origin": "https://app.example"}
    )
    assert ok.status_code == 201
    pre = prod_client.options(
        f"{API}/auth/me",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-csrf-token",
        },
    )
    assert pre.headers.get("access-control-allow-origin") == "https://app.example"
    assert pre.headers.get("access-control-allow-credentials") == "true"
    bad = prod_client.options(
        f"{API}/auth/me",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert bad.headers.get("access-control-allow-origin") is None


# ------------------------------------------------------------------ CSRF
def test_csrf_required_for_cookie_writes(client, alice):
    _, cookies = _signup_cookies(client)
    url = f"{API}/auth/api-tokens"
    # safe method: no CSRF header needed
    assert send(client, "GET", url, cookies, csrf=False).status_code == 200
    # missing header
    r = send(client, "POST", url, cookies, csrf=False, json={"name": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    # header present but different from the cookie
    r = send(
        client,
        "POST",
        url,
        cookies,
        csrf=False,
        json={"name": "x"},
        headers={"X-CSRF-Token": "abc.def"},
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    # header equal to a cookie the attacker planted (not bound to this session)
    planted = {**cookies, "jt_csrf": "abc.def"}
    r = send(client, "POST", url, planted, json={"name": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    # a valid CSRF token of another session does not work for this one
    other = login_session(client, "c@c.example")
    r = send(client, "POST", url, {**cookies, "jt_csrf": other["jt_csrf"]}, json={"name": "x"})
    assert r.status_code == 403
    # matching header + cookie of this session
    r = send(client, "POST", url, cookies, json={"name": "ok"})
    assert r.status_code == 201, r.text
    # DELETE is unsafe too
    tid = r.json()["id"]
    r = send(client, "DELETE", f"{url}/{tid}", cookies, csrf=False)
    assert r.status_code == 403
    assert send(client, "DELETE", f"{url}/{tid}", cookies).status_code == 204


def test_bearer_requests_are_exempt_from_csrf(client, alice):
    # alice is a Bearer access token (no cookies): POST without X-CSRF-Token works
    r = client.post(f"{API}/auth/api-tokens", headers=alice, json={"name": "cli", "role": "admin"})
    assert r.status_code == 201
    # an API token (CLI / MCP) writes without X-CSRF-Token as well
    api = {"Authorization": f"Bearer {r.json()['token']}"}
    r2 = client.post(f"{API}/auth/api-tokens", headers=api, json={"name": "x", "role": "viewer"})
    assert r2.status_code == 201
    # an invalid Bearer header is not rescued by a valid cookie
    _, cookies = _signup_cookies(client, "z@z.example", "Z")
    r3 = send(
        client, "GET", f"{API}/auth/me", cookies, headers={"Authorization": "Bearer jtk_invalid"}
    )
    assert r3.status_code == 401
    # an API token is never accepted from the access cookie
    r4 = send(client, "GET", f"{API}/auth/me", {"jt_access": r.json()["token"]})
    assert r4.status_code == 401


def test_refresh_and_logout_require_csrf(client):
    _, cookies = _signup_cookies(client)
    r = send(client, "POST", f"{API}/auth/refresh", cookies, csrf=False)
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    r = send(client, "POST", f"{API}/auth/logout", cookies, csrf=False)
    assert r.status_code == 403
    # neither consumed nor ended the session
    assert send(client, "POST", f"{API}/auth/refresh", cookies).status_code == 200


# ------------------------------------------------------------------ refresh / logout
def test_logout_then_refresh_and_access_fail(client):
    _, cookies = _signup_cookies(client)
    assert send(client, "GET", f"{API}/auth/me", cookies).status_code == 200
    assert send(client, "POST", f"{API}/auth/logout", cookies).status_code == 204
    r = send(client, "POST", f"{API}/auth/refresh", cookies)
    assert r.status_code == 401
    # the access cookie (still within its TTL) is dead too: the session ended
    r = send(client, "GET", f"{API}/auth/me", cookies)
    assert r.status_code == 401 and r.json()["error"]["code"] == "session_ended"
    # and as Bearer
    r = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {cookies['jt_access']}"})
    assert r.status_code == 401
    # logout without any session cookie just clears cookies
    assert client.post(f"{API}/auth/logout").status_code == 204


def test_reused_refresh_revokes_the_family(client, clock, settings):
    _, first = _signup_cookies(client)
    r1 = send(client, "POST", f"{API}/auth/refresh", first)
    assert r1.status_code == 200
    second = {**first, **session_cookies(r1)}
    assert send(client, "GET", f"{API}/auth/me", second).status_code == 200
    clock.advance(settings.refresh_reuse_grace_s + 1)
    replay = send(client, "POST", f"{API}/auth/refresh", first)
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "refresh_token_reused"
    # every token of the family is dead: the rotated refresh token and the new access token
    assert send(client, "POST", f"{API}/auth/refresh", second).status_code == 401
    assert send(client, "GET", f"{API}/auth/me", second).status_code == 401
    # other sessions of the same user are not affected
    other = login_session(client, "c@c.example")
    assert send(client, "GET", f"{API}/auth/me", other).status_code == 200


def test_session_absolute_lifetime(client, rt, settings, clock, ingest, make_client):
    short = settings.model_copy(update={"session_max_age_s": 3000, "access_token_ttl_s": 900})
    r2 = Runtime.build(short, clock=clock, ingest=ingest)
    try:
        c = make_client(r2)
        _, cookies = _signup_cookies(c, "s@s.example", "S")
        for _ in range(3):
            clock.advance(900)
            r = send(c, "POST", f"{API}/auth/refresh", cookies)
            assert r.status_code == 200, r.text
            cookies = {**cookies, **session_cookies(r)}
        clock.advance(400)  # 3100 s after login
        r = send(c, "POST", f"{API}/auth/refresh", cookies)
        assert r.status_code == 401
        assert r.json()["error"]["code"] in ("refresh_token_expired", "session_expired")
    finally:
        r2.close()


def test_access_token_without_session_claim_is_rejected(client, alice, rt):
    """Tokens minted before revision 0005_auth_sessions carry no ``sid``."""
    token = alice["Authorization"].split()[1]
    claims = jwt.decode(token, options={"verify_signature": False})
    claims.pop("sid")
    legacy = jwt.encode(claims, rt.settings.jwt_secret, algorithm="HS256")
    r = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {legacy}"})
    assert r.status_code == 401


# ------------------------------------------------------------------ tenant isolation
def test_valid_cookies_cannot_reach_another_tenant(client, worker):
    bob = signup(client, "bob@b.example", "나 회사")
    up = client.post(
        f"{API}/documents",
        headers=bob,
        files={"file": ("ledger.csv", LEDGER_CSV.encode(), "text/csv")},
        data={"kind": "bank"},
    )
    assert up.status_code == 201, up.text
    worker.run_once()
    dv, doc_id = up.json()["document"]["id"], up.json()["document"]["document_id"]
    assert client.get(f"{API}/decisions/dec:I1", headers=bob).status_code == 200

    _, alice = _signup_cookies(client, "alice@a.example", "가 회사")
    me = send(client, "GET", f"{API}/auth/me", alice).json()
    assert me["tenant"]["name"] == "가 회사"
    for path in (
        f"{API}/document-versions/{dv}",
        f"{API}/documents/{doc_id}/versions",
        f"{API}/decisions/dec:I1",
    ):
        r = send(client, "GET", path, alice)
        assert r.status_code == 404, (path, r.status_code)
    assert send(client, "GET", f"{API}/decisions", alice).json()["items"] == []
    # a client-sent tenant id is ignored: still alice's tenant
    r = send(client, "GET", f"{API}/decisions?tenant_id={me['tenant_id']}x", alice)
    assert r.status_code in (200, 422) and r.json().get("items", []) == []
    # a write with valid cookies + CSRF on bob's decision is 404, not applied
    r = send(
        client,
        "POST",
        f"{API}/decisions/dec:I1/approvals",
        alice,
        json={"expected_result_hash": "0" * 64},
    )
    assert r.status_code == 404


def test_login_with_tenant_selection_scopes_the_session(client, rt):
    """A user in two tenants gets a session for exactly the tenant chosen at login."""
    import uuid

    import sqlalchemy as sa

    from jettae.db.orm import MembershipRow, TenantRow, UserRow

    _, first = _signup_cookies(client, "multi@m.example", "첫째")
    with rt.db.write() as s:
        uid = s.scalars(sa.select(UserRow.id).where(UserRow.email == "multi@m.example")).one()
        s.add(TenantRow(id="tn_second", name="둘째"))
        s.flush()
        s.add(
            MembershipRow(
                id=f"mem_{uuid.uuid4().hex}", tenant_id="tn_second", user_id=uid, role="viewer"
            )
        )
    r = client.post(f"{API}/auth/login", json={"email": "multi@m.example", "password": PASSWORD})
    client.cookies.clear()
    assert r.status_code == 409 and r.json()["error"]["code"] == "tenant_selection_required"
    second = login_session(client, "multi@m.example", tenant_id="tn_second")
    me = send(client, "GET", f"{API}/auth/me", second).json()
    assert me["tenant"]["id"] == "tn_second" and me["user"]["role"] == "viewer"
    # viewer session: the CSRF-valid write is still refused by role
    r = send(client, "POST", f"{API}/auth/api-tokens", second, json={"name": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    assert send(client, "GET", f"{API}/auth/me", first).json()["tenant"]["name"] == "첫째"


def test_bearer_helper_matches_cookie_identity(client):
    r = client.post(
        f"{API}/auth/signup",
        json={"email": "h@h.example", "password": PASSWORD, "tenant_name": "H"},
    )
    cookies = session_cookies(r)
    h = bearer_from_cookies(client, r)
    a = client.get(f"{API}/auth/me", headers=h).json()
    b = send(client, "GET", f"{API}/auth/me", cookies).json()
    assert a["user"] == b["user"] and a["tenant"] == b["tenant"]
    assert (a["auth_method"], b["auth_method"]) == ("jwt", "cookie")


# ------------------------------------------------------------------ overlapping tabs, throttling
def test_two_tabs_refreshing_with_one_cookie_keep_the_session(client, clock, settings):
    """Tabs share one cookie jar; when their common access cookie expires both can send the
    same refresh cookie before the first Set-Cookie lands. The second must not end the
    session (reuse grace), and must not fork the refresh family."""
    _, first = _signup_cookies(client)
    tab_a = send(client, "POST", f"{API}/auth/refresh", first)
    assert tab_a.status_code == 200
    rotated = {**first, **session_cookies(tab_a)}
    assert rotated["jt_refresh"] != first["jt_refresh"]
    clock.advance(2)
    tab_b = send(client, "POST", f"{API}/auth/refresh", first)
    assert tab_b.status_code == 200, tab_b.text
    # access cookie only: the jar keeps tab A's successor refresh token
    assert "jt_refresh" not in _set_cookies(tab_b)
    assert tab_b.cookies.get("jt_access")
    _no_tokens_in(tab_b.json(), {**first, **session_cookies(tab_b)})
    b_cookies = {**rotated, "jt_access": tab_b.cookies["jt_access"]}
    assert send(client, "GET", f"{API}/auth/me", b_cookies).status_code == 200
    # tab A's freshly rotated cookies still work, and its refresh token rotates normally
    assert send(client, "GET", f"{API}/auth/me", rotated).status_code == 200
    again = send(client, "POST", f"{API}/auth/refresh", rotated)
    assert again.status_code == 200 and again.cookies.get("jt_refresh")
    latest = {**rotated, **session_cookies(again)}
    # past the window the old token is theft again: the whole session ends
    clock.advance(settings.refresh_reuse_grace_s + 1)
    late = send(client, "POST", f"{API}/auth/refresh", first)
    assert late.status_code == 401 and late.json()["error"]["code"] == "refresh_token_reused"
    assert send(client, "GET", f"{API}/auth/me", latest).status_code == 401


def test_reuse_grace_never_revives_a_logged_out_session(client):
    _, first = _signup_cookies(client)
    r1 = send(client, "POST", f"{API}/auth/refresh", first)
    second = {**first, **session_cookies(r1)}
    assert send(client, "POST", f"{API}/auth/logout", second).status_code == 204
    # within the window, but the session ended: no new access token
    r = send(client, "POST", f"{API}/auth/refresh", first)
    assert r.status_code == 401 and not r.cookies.get("jt_access")


def test_reuse_grace_still_needs_the_csrf_header(client):
    _, first = _signup_cookies(client)
    assert send(client, "POST", f"{API}/auth/refresh", first).status_code == 200
    r = send(client, "POST", f"{API}/auth/refresh", first, csrf=False)
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"


def test_reuse_grace_zero_keeps_strict_reuse_detection(rt, settings, clock, ingest, make_client):
    strict = settings.model_copy(update={"refresh_reuse_grace_s": 0})
    r2 = Runtime.build(strict, clock=clock, ingest=ingest)
    try:
        c = make_client(r2)
        _, first = _signup_cookies(c, "g@g.example", "G")
        assert send(c, "POST", f"{API}/auth/refresh", first).status_code == 200
        replay = send(c, "POST", f"{API}/auth/refresh", first)
        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "refresh_token_reused"
    finally:
        r2.close()


def test_failed_logins_from_one_ip_do_not_block_other_users_refresh(client, settings):
    """All browsers can share one client IP (Next.js rewrite, Docker port publishing). An
    attacker's failed logins from that IP must not make every user's refresh fail."""
    _, victim = _signup_cookies(client, "v@v.example", "V")
    codes = [
        client.post(
            f"{API}/auth/login", json={"email": f"x{i}@x.example", "password": PASSWORD + "-no"}
        ).status_code
        for i in range(settings.auth_ip_per_minute + 1)
    ]
    assert codes[-1] == 429 and set(codes) == {401, 429}  # the IP bucket is exhausted
    r = send(client, "POST", f"{API}/auth/refresh", victim)
    assert r.status_code == 200, r.text


def test_refresh_is_limited_per_session_not_per_ip(rt, settings, clock, ingest, make_client):
    tight = settings.model_copy(update={"refresh_per_minute": 3, "refresh_reuse_grace_s": 0})
    r2 = Runtime.build(tight, clock=clock, ingest=ingest)
    try:
        c = make_client(r2)
        _, cur = _signup_cookies(c, "p@p.example", "P")
        _, other = _signup_cookies(c, "q@q.example", "Q")
        for _ in range(3):
            r = send(c, "POST", f"{API}/auth/refresh", cur)
            assert r.status_code == 200
            cur = {**cur, **session_cookies(r)}
        limited = send(c, "POST", f"{API}/auth/refresh", cur)
        assert limited.status_code == 429 and limited.headers.get("Retry-After")
        # another session from the same client IP is not affected
        assert send(c, "POST", f"{API}/auth/refresh", other).status_code == 200
        # the limited request consumed nothing: the same cookie works after the window
        clock.advance(61)
        assert send(c, "POST", f"{API}/auth/refresh", cur).status_code == 200
    finally:
        r2.close()
