"""MCP server: same tools, API-token auth, tenant resolved server-side, as_of, roles."""

from __future__ import annotations

import json
from typing import Any

import pytest
from la_helpers import seed_tenant

from jettae.agents import AgentStore, HeuristicPlanner, run_agent
from jettae.agents.session import AgentSession, AuthFailed
from jettae.agents.tools import TOOLS
from jettae.api.auth import AuthService
from jettae.mcp_server import ApiTokenVerifier, build_server, http_principal_resolver

PASSWORD = "correct horse battery"


@pytest.fixture
def world(rt) -> dict[str, Any]:
    auth = AuthService(rt.db, rt.settings, clock=rt.clock)
    out: dict[str, Any] = {"auth": auth}
    for key, email, name, prefix in (
        ("a", "a@example.com", "A상사", ""),
        ("b", "b@example.com", "B상사", "B"),
    ):
        pair = auth.signup(email, PASSWORD, name)
        p = auth.authenticate(pair.access_token)
        _, member = auth.create_api_token(p, "mcp", "member", None)
        _, viewer = auth.create_api_token(p, "ro", "viewer", None)
        seed_tenant(rt.service, p.tenant_id, prefix=prefix)
        out[key] = {"tenant": p.tenant_id, "member": member, "viewer": viewer}
    out["session"] = AgentSession(rt, auth, AgentStore())
    return out


def server_for(world: dict[str, Any], rt, token: str):
    s: AgentSession = world["session"]
    return build_server(lambda: rt.service, lambda: s.principal(token), s.store)


def call(portal, server, name: str, args: dict[str, Any]) -> tuple[bool, Any]:
    from mcp import Client

    async def go() -> Any:
        async with Client(server) as c:
            return await c.call_tool(name, args)

    res = portal.call(go)
    if res.is_error:
        return False, res.content[0].text
    if res.structured_content is not None:
        return True, res.structured_content
    return True, json.loads(res.content[0].text)


def list_tools(portal, server) -> list[str]:
    from mcp import Client

    async def go() -> Any:
        async with Client(server) as c:
            return await c.list_tools()

    return sorted(t.name for t in portal.call(go).tools)


def test_mcp_exposes_exactly_the_agent_tools(world, rt, portal):
    srv = server_for(world, rt, world["a"]["member"])
    assert list_tools(portal, srv) == sorted(TOOLS)


def test_tenant_comes_from_token_and_tenant_args_are_ignored(world, rt, portal):
    a, b = world["a"], world["b"]
    srv_a = server_for(world, rt, a["member"])
    ok, res = call(
        portal, srv_a, "calculate_due", {"decision_id": "dec:I1", "tenant_id": b["tenant"]}
    )
    assert ok and res["decision_id"] == "dec:I1"
    assert (
        res["result_hash"]
        == rt.service.repos.decisions.get_current(a["tenant"], "dec:I1").result_hash
    )
    # B's decision is invisible to A, whatever tenant id the client sends
    ok, err = call(
        portal, srv_a, "calculate_due", {"decision_id": "dec:BI1", "tenant_id": b["tenant"]}
    )
    assert not ok and "not_found" in err
    ok, err = call(portal, srv_a, "get_source_span", {"fact_id": "f-BI1"})
    assert not ok and "not_found" in err
    ok, docs = call(portal, srv_a, "search_documents", {"tenant_id": b["tenant"]})
    assert ok and docs["count"] == 2
    assert all(not d["untrusted_filename"].startswith("B") for d in docs["documents"])
    # B's token sees B's data only
    ok, res = call(
        portal,
        server_for(world, rt, b["member"]),
        "reconcile_transactions",
        {"decision_id": "dec:BI1"},
    )
    assert ok and res["subject_id"] == "BI1"


def test_invalid_or_revoked_token_is_unauthorized(world, rt, portal):
    ok, err = call(portal, server_for(world, rt, "jtk_not-a-token"), "search_documents", {})
    assert not ok and "unauthorized" in err
    s: AgentSession = world["session"]
    with pytest.raises(AuthFailed):
        s.principal(None)


def test_viewer_cannot_store_drafts_member_can(world, rt, portal):
    a = world["a"]
    dec = rt.service.repos.decisions.get_current(a["tenant"], "dec:I2")
    args = {"decision_id": "dec:I2", "document": dec.required_documents[0], "reason": "확인"}
    ok, err = call(portal, server_for(world, rt, a["viewer"]), "propose_missing_evidence", args)
    assert not ok and "forbidden" in err
    ok, res = call(
        portal, server_for(world, rt, a["viewer"]), "validate_evidence", {"decision_id": "dec:I2"}
    )
    assert ok and res["review_status"] in ("VERIFIED", "DRAFT")
    ok, res = call(portal, server_for(world, rt, a["member"]), "propose_missing_evidence", args)
    assert ok and res["sent"] is False and res["tenant_id"] == a["tenant"]
    assert rt.service.repos.approvals.list_for(a["tenant"], "dec:I2") == []


def test_as_of_argument(world, rt, portal):
    srv = server_for(world, rt, world["a"]["member"])
    ok, res = call(portal, srv, "calculate_due", {"decision_id": "dec:I1", "as_of": "2025-12-31"})
    assert ok and res["dry_run"] is True and res["as_of"] == "2025-12-31"
    ok, err = call(portal, srv, "calculate_due", {"decision_id": "dec:I1", "as_of": "31/12/2025"})
    assert not ok and "as_of" in err


def test_run_status_through_mcp_is_tenant_scoped(world, rt, portal):
    s: AgentSession = world["session"]
    a, b = world["a"], world["b"]
    rep = run_agent(
        rt.service,
        a["tenant"],
        "dec:I1",
        strategy="roles",
        planner=HeuristicPlanner(),
        store=s.store,
    )
    ok, res = call(
        portal, server_for(world, rt, a["member"]), "get_run_status", {"run_id": rep.run_id}
    )
    assert ok and res["status"] == "COMPLETED" and res["strategy"] == "roles"
    ok, err = call(
        portal, server_for(world, rt, b["member"]), "get_run_status", {"run_id": rep.run_id}
    )
    assert not ok and "not_found" in err


def test_http_token_verifier_and_per_request_resolver(world, rt, portal):
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    s: AgentSession = world["session"]
    a = world["a"]
    verifier = ApiTokenVerifier(s.principal)

    async def verify(tok: str) -> Any:
        return await verifier.verify_token(tok)

    at = portal.call(verify, a["member"])
    assert at is not None and at.claims["tid"] == a["tenant"] and at.scopes == ["member"]
    assert portal.call(verify, "jtk_bad") is None
    resolve = http_principal_resolver(s.principal)
    with pytest.raises(PermissionError):
        resolve()
    token = auth_context_var.set(AuthenticatedUser(at))
    try:
        assert resolve().tenant_id == a["tenant"]
    finally:
        auth_context_var.reset(token)
