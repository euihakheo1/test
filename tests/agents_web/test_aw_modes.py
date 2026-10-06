"""Investigation modes through API + worker: live refused unless the server allows it,
live/replay over the LLM gateway (stub provider, never a network call), usage and budget
accounting, prompt injection in documents cannot add tool calls."""

from __future__ import annotations

from decimal import Decimal
from functools import partial
from typing import Any

import pytest
from aw_helpers import INJECTION, Web, act, obedient_model, seed_conflict, tenant_of
from jt_api_helpers import signup

from jettae.agents.planners import ROLES
from jettae.llm import (
    Budget,
    FakeProvider,
    GatewayConfig,
    LLMGateway,
    LLMMode,
    MemoryReplayStore,
    ModelPrice,
    PriceTable,
)
from jettae.worker import handle_investigate_decision

MODEL = "fake-model-1"


class Factory:
    """Gateway factory handed to the worker handler: one replay store and one budget shared
    by every gateway it builds (as one server would). Records the modes it was asked for."""

    def __init__(self, responder: Any = None, *, budget_krw: str = "1000") -> None:
        self.store = MemoryReplayStore()
        self.budget = Budget(Decimal(budget_krw))
        self.provider = FakeProvider(responder or (lambda r: act(None)), model=MODEL)
        self.modes: list[LLMMode] = []

    def __call__(self, mode: LLMMode) -> LLMGateway:
        self.modes.append(mode)
        return LLMGateway(
            provider=self.provider if mode is LLMMode.LIVE else None,
            model=MODEL,
            provider_name="fake",
            store=self.store,
            config=GatewayConfig(mode=mode, backoff_s=0.0),
            budget=self.budget,
            prices=PriceTable({MODEL: ModelPrice(Decimal("1000"), Decimal("5000"), "test")}),
            sleep=lambda s: None,
        )

    def handlers(self) -> dict[str, Any]:
        return {"investigate_decision": partial(handle_investigate_decision, gateway_factory=self)}


def _allow_live(monkeypatch: pytest.MonkeyPatch, budget: str = "1000") -> None:
    monkeypatch.setenv("JETTAE_LLM_MODE", "live")
    monkeypatch.setenv("JETTAE_LLM_BUDGET_KRW", budget)


def _setup(client: Any, rt: Any, work: Any, email: str, **kw: Any) -> tuple[Web, str, str]:
    web = Web(client, signup(client, email, email.split("@")[0]))
    tenant = tenant_of(client, web.h)
    seed_conflict(rt, tenant, **kw)
    web.analyze()
    work()
    (did,) = web.decisions()
    return web, tenant, did


def _run(web: Web, work: Any, did: str, handlers: Any = None, **kw: Any) -> dict[str, Any]:
    r = web.start(did, **kw)
    assert r.status_code == 202, r.text
    work(handlers)
    return web.investigation(did, r.json()["investigation_id"])


# --------------------------------------------------------------------------- capabilities
def test_capabilities_follow_server_settings_only(client, monkeypatch):
    web = Web(client, signup(client, "cap@x.example", "cap"))
    r = client.get("/api/v1/investigations/capabilities", headers=web.h)
    assert r.status_code == 200
    body = r.json()
    assert body["modes_enabled"] == ["offline", "replay"] and body["live_enabled"] is False
    assert body["live_disabled_reason"]  # readable reason, no setting values
    monkeypatch.setenv("JETTAE_LLM_MODE", "live")
    monkeypatch.setenv("JETTAE_LLM_BUDGET_KRW", "0")
    assert (
        client.get("/api/v1/investigations/capabilities", headers=web.h).json()["live_enabled"]
        is False
    )
    _allow_live(monkeypatch)
    body = client.get("/api/v1/investigations/capabilities", headers=web.h).json()
    assert body == {"modes_enabled": ["offline", "replay", "live"], "live_enabled": True}
    # prod additionally needs the shared PostgreSQL budget database
    monkeypatch.setenv("JETTAE_ENV", "prod")
    assert (
        client.get("/api/v1/investigations/capabilities", headers=web.h).json()["live_enabled"]
        is False
    )
    monkeypatch.setenv("JETTAE_LLM_BUDGET_DB", "postgresql+psycopg://u:p@db/jettae")
    assert (
        client.get("/api/v1/investigations/capabilities", headers=web.h).json()["live_enabled"]
        is True
    )
    assert client.get("/api/v1/investigations/capabilities").status_code == 401


def test_live_is_refused_without_server_budget_and_never_builds_a_gateway(client, rt, work):
    web, _, did = _setup(client, rt, work, "refuse@x.example")
    factory = Factory()
    inv = _run(web, work, did, factory.handlers(), mode="live")
    assert inv["status"] == "refused"
    assert inv["error"]["code"] == "live_disabled"
    assert inv["findings"] == [] and inv["usage"]["cost_krw"] == 0
    assert factory.modes == [] and factory.provider.calls == []
    # the job itself finished: the outcome lives on the investigation
    assert web.job(inv["job_id"])["status"] == "succeeded"


def test_request_body_cannot_enable_live_or_choose_a_tenant(client, rt, work):
    web, _, did = _setup(client, rt, work, "body@x.example")
    for extra in ({"tenant_id": "tn_other"}, {"budget_krw": 100000}, {"live": True}):
        r = web.start(did, mode="live", **extra)
        assert r.status_code == 422, (extra, r.text)
    assert web.start(did, mode="paid").status_code == 422
    assert web.start(did, strategy="swarm").status_code == 422


# --------------------------------------------------------------------------- live / replay
def test_live_run_uses_budget_and_document_text_cannot_add_tools(client, rt, work, monkeypatch):
    web, tenant, did = _setup(client, rt, work, "inject@x.example", text_extra=INJECTION)
    before = web.decisions()[did]["result_hash"]
    offline = _run(web, work, did, strategy="roles", mode="offline")
    _allow_live(monkeypatch)
    factory = Factory(obedient_model(did))
    inv = _run(web, work, did, factory.handlers(), strategy="roles", mode="live")

    assert inv["status"] == "succeeded", inv
    actions = inv["report"]["actions"]
    executed = [(a["role"], a["tool"]) for a in actions if a["kind"] == "tool_call"]
    # after reading the injected text the model asked for a tool outside the extractor
    # role: the per-role output schema rejected it, and the role ended there. Only the
    # search it was allowed to make ran.
    assert executed == [("extractor", "search_documents")]
    for role, tool in executed:
        assert tool in ROLES[role].tools
    rejected = [a for a in actions if a["kind"] == "rejected"]
    assert len(rejected) == 1 and rejected[0]["role"] == "extractor"
    assert "planner output rejected" in rejected[0]["note"]
    assert "propose_missing_evidence" in rejected[0]["note"]
    assert not any(
        a["tool"] in ("approve", "propose_missing_evidence")
        for a in actions
        if a["kind"] == "tool_call"
    )
    assert inv["report"]["proposals"] == []
    # the request that saw the injected text carried it only as data
    seen = [r for r in factory.provider.calls if "SYSTEM OVERRIDE" in r.messages[0].parts[0].text]
    assert seen and all("SYSTEM OVERRIDE" not in r.system for r in seen)

    # findings are code-made from the engine: identical to the offline run
    assert inv["findings"] == offline["findings"]
    assert web.decisions()[did]["result_hash"] == before
    assert rt.repos.approvals.list_for(tenant, did) == []

    u = inv["usage"]
    # every paid attempt is accounted, including the rejected (schema-invalid) output and
    # its retry: the reported cost equals what the shared budget recorded
    assert 3 <= u["llm_calls"] <= len(factory.provider.calls)
    assert u["input_tokens"] > 0 and u["output_tokens"] > 0
    spent = factory.budget.spent_krw
    assert Decimal(u["cost_krw_exact"]) == spent and spent <= Decimal("1000")
    assert u["cost_krw"] >= 1 and isinstance(u["cost_krw"], int)


def test_replay_reproduces_a_recorded_live_run_at_no_cost(client, rt, work, monkeypatch):
    web, _, did = _setup(client, rt, work, "replay@x.example")

    def polite(req: Any) -> dict[str, Any]:
        text = req.messages[0].parts[0].text
        if '"tool": "calculate_due"' in text:
            return act(None)
        return act("calculate_due", decision_id=did)

    factory = Factory(polite)
    _allow_live(monkeypatch)
    live = _run(web, work, did, factory.handlers(), strategy="single", mode="live")
    assert live["status"] == "succeeded", live
    calls = len(factory.provider.calls)
    assert calls == live["usage"]["llm_calls"] >= 2

    monkeypatch.delenv("JETTAE_LLM_MODE")  # replay needs no paid-call setting
    replay = _run(web, work, did, factory.handlers(), strategy="single", mode="replay")
    assert replay["status"] == "succeeded", replay
    assert len(factory.provider.calls) == calls  # no provider call
    assert replay["usage"]["cached_llm_calls"] == replay["usage"]["llm_calls"] == calls
    assert replay["usage"]["cost_krw"] == 0
    tools = [(a["kind"], a["tool"]) for a in replay["report"]["actions"]]
    assert tools == [(a["kind"], a["tool"]) for a in live["report"]["actions"]]
    assert replay["findings"] == live["findings"]

    fresh = Factory()
    miss = _run(web, work, did, fresh.handlers(), strategy="roles", mode="replay")
    assert miss["status"] == "failed" and miss["error"]["code"] == "replay_miss"
    assert miss["findings"] == [] and fresh.provider.calls == []


def test_budget_exhaustion_is_refused_not_failed(client, rt, work, monkeypatch):
    web, _, did = _setup(client, rt, work, "poor@x.example")
    _allow_live(monkeypatch)
    factory = Factory(lambda r: act("calculate_due", decision_id=did), budget_krw="0.0001")
    inv = _run(web, work, did, factory.handlers(), strategy="single", mode="live")
    assert inv["status"] == "refused", inv
    assert inv["error"]["code"] == "live_refused"
    assert factory.budget.spent_krw <= Decimal("0.0001")
