"""Signup / login / refresh rotation / expiry / API tokens / roles (cookie sessions; the
CSRF, logout and cookie-attribute rules are in ``test_auth_cookies.py``)."""

from __future__ import annotations

import sqlalchemy as sa
from jt_api_helpers import PASSWORD, login_session, send, session_cookies

from jettae.api.auth import sha256_hex
from jettae.db.orm import ApiTokenRow, RefreshTokenRow, UserRow

API = "/api/v1"


def test_signup_login_me(client, rt):
    r = client.post(
        f"{API}/auth/signup",
        json={"email": " Kim@Example.COM ", "password": PASSWORD, "tenant_name": "가나상사"},
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["role"] == "owner" and out["user"]["email"] == "kim@example.com"
    assert out["tenant"]["name"] == "가나상사"
    cookies = session_cookies(r)
    me = send(client, "GET", f"{API}/auth/me", cookies)
    assert me.json()["email"] == "kim@example.com" and me.json()["tenant_name"] == "가나상사"
    assert me.json()["user"]["id"] == out["user"]["id"]
    assert me.json()["tenant"] == {"id": out["tenant_id"], "name": "가나상사"}
    assert me.json()["auth_method"] == "cookie"
    with rt.db.session() as s:
        u = s.scalars(sa.select(UserRow)).one()
        assert u.password_hash.startswith("$argon2id$") and PASSWORD not in u.password_hash
        rts = list(s.scalars(sa.select(RefreshTokenRow)))
        assert rts[0].token_hash == sha256_hex(cookies["jt_refresh"])  # only the hash stored

    dup = client.post(
        f"{API}/auth/signup",
        json={"email": "kim@example.com", "password": PASSWORD, "tenant_name": "x"},
    )
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "email_taken"
    weak = client.post(
        f"{API}/auth/signup", json={"email": "a@b.example", "password": "short", "tenant_name": "x"}
    )
    assert weak.status_code == 422 and weak.json()["error"]["code"] == "weak_password"

    ok = client.post(f"{API}/auth/login", json={"email": "KIM@example.com", "password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["tenant_id"] == out["tenant_id"]
    for body in (
        {"email": "kim@example.com", "password": "wrong password!!"},
        {"email": "nobody@example.com", "password": PASSWORD},
        {"email": "not-an-email", "password": PASSWORD},
    ):
        bad = client.post(f"{API}/auth/login", json=body)
        assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_credentials"
    other = client.post(
        f"{API}/auth/login",
        json={"email": "kim@example.com", "password": PASSWORD, "tenant_id": "tn_other"},
    )
    assert other.status_code == 403


def test_refresh_rotation_and_reuse_detection(client):
    first = session_cookies(
        client.post(
            f"{API}/auth/signup",
            json={"email": "r@r.example", "password": PASSWORD, "tenant_name": "R"},
        )
    )
    r1 = send(client, "POST", f"{API}/auth/refresh", first)
    assert r1.status_code == 200
    new = {**first, **session_cookies(r1)}
    assert new["jt_refresh"] != first["jt_refresh"]
    assert new["jt_access"] != first["jt_access"]
    # replaying the rotated token: rejected and the whole family revoked
    replay = send(client, "POST", f"{API}/auth/refresh", first)
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "refresh_token_reused"
    stolen = send(client, "POST", f"{API}/auth/refresh", new)
    assert stolen.status_code == 401
    # logout revokes
    tok2 = login_session(client, "r@r.example")
    assert send(client, "POST", f"{API}/auth/logout", tok2).status_code == 204
    r = send(client, "POST", f"{API}/auth/refresh", tok2)
    assert r.status_code == 401
    garbage = {**tok2, "jt_refresh": "jtr_garbage"}
    assert send(client, "POST", f"{API}/auth/refresh", garbage).status_code == 401


def test_access_and_refresh_expiry(client, clock, rt):
    tok = session_cookies(
        client.post(
            f"{API}/auth/signup",
            json={"email": "e@e.example", "password": PASSWORD, "tenant_name": "E"},
        )
    )
    h = {"Authorization": f"Bearer {tok['jt_access']}"}
    assert client.get(f"{API}/auth/me", headers=h).status_code == 200
    clock.advance(rt.settings.access_token_ttl_s + 1)
    r = client.get(f"{API}/auth/me", headers=h)
    assert r.status_code == 401 and r.json()["error"]["code"] == "token_expired"
    assert r.headers.get("www-authenticate") is None or "Bearer" in r.headers["www-authenticate"]
    fresh = send(client, "POST", f"{API}/auth/refresh", tok)
    assert fresh.status_code == 200
    clock.advance(rt.settings.refresh_token_ttl_s + 1)
    r = send(client, "POST", f"{API}/auth/refresh", {**tok, **session_cookies(fresh)})
    assert r.status_code == 401 and r.json()["error"]["code"] == "refresh_token_expired"


def test_api_tokens(client, alice, rt, clock):
    r = client.post(
        f"{API}/auth/api-tokens",
        headers=alice,
        json={"name": "mcp", "role": "member", "expires_in_days": 1},
    )
    assert r.status_code == 201, r.text
    created = r.json()
    raw = created["token"]
    assert raw.startswith("jtk_")
    with rt.db.session() as s:
        row = s.get(ApiTokenRow, created["id"])
        assert row.token_hash == sha256_hex(raw) and raw not in row.token_hash
    h = {"Authorization": f"Bearer {raw}"}
    me = client.get(f"{API}/auth/me", headers=h).json()
    assert me["auth_method"] == "api_token" and me["role"] == "member"
    lst = client.get(f"{API}/auth/api-tokens", headers=alice).json()
    assert [t["id"] for t in lst] == [created["id"]] and "token" not in lst[0]
    # member token cannot mint tokens (admin+ only)
    r = client.post(f"{API}/auth/api-tokens", headers=h, json={"name": "x", "role": "viewer"})
    assert r.status_code == 403
    # revocation
    r2 = client.post(f"{API}/auth/api-tokens", headers=alice, json={"name": "cli"}).json()
    h2 = {"Authorization": f"Bearer {r2['token']}"}
    assert client.get(f"{API}/auth/me", headers=h2).status_code == 200
    assert client.delete(f"{API}/auth/api-tokens/{r2['id']}", headers=alice).status_code == 204
    assert client.get(f"{API}/auth/me", headers=h2).status_code == 401
    # expiry (the access JWT of alice expires too; the API token outlives it until its date)
    clock.advance(3600)
    assert client.get(f"{API}/auth/me", headers=alice).status_code == 401
    assert client.get(f"{API}/auth/me", headers=h).status_code == 200
    clock.advance(2 * 86400)
    r = client.get(f"{API}/auth/me", headers=h)
    assert r.status_code == 401 and r.json()["error"]["code"] == "token_expired"


def test_other_tenant_cannot_revoke_tokens(client, alice, bob):
    t = client.post(f"{API}/auth/api-tokens", headers=alice, json={"name": "a"}).json()
    assert client.delete(f"{API}/auth/api-tokens/{t['id']}", headers=bob).status_code == 404
    assert client.get(f"{API}/auth/api-tokens", headers=bob).json() == []
