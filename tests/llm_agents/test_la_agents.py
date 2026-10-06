"""Agent flows: single vs roles over the same tools; the LLM never changes engine numbers;
prompt injection in documents is data; caps; tenant scoping of tools."""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from datetime import date
from typing import Any

import pytest
from conftest import OTHER, T
from la_helpers import AS_OF, INJECTION, gateway

from jettae.agents import (
    TOOLS,
    AgentLimits,
    AgentStore,
    HeuristicPlanner,
    LLMPlanner,
    ToolContext,
    ToolError,
    call_tool,
    langgraph_available,
    run_agent,
)
from jettae.agents.planners import ARG_FIELDS, ROLES, SYSTEM_PROMPT, PlannedAction
from jettae.agents.tools import jsonable
from jettae.llm import FakeProvider, LLMMode, LLMRequest

EXPECTED_TOOLS = {
    "search_documents",
    "get_source_span",
    "list_transaction_candidates",
    "reconcile_transactions",
    "calculate_due",
    "get_agreement_conditions",
    "validate_evidence",
    "propose_recompute",
    "get_run_status",
    "propose_missing_evidence",
}


def act(tool: str | None = None, summary: str = "", **args: Any) -> dict[str, Any]:
    full = {f: args.get(f) for f in ARG_FIELDS}
    if tool is None:
        return {"action": "finish", "tool": None, "args": full, "summary": summary or "완료"}
    return {"action": "call_tool", "tool": tool, "args": full, "summary": summary or tool}


def scripted(script: dict[str, list[dict[str, Any]]]):
    queues = {k: list(v) for k, v in script.items()}
    seen: dict[str, list[LLMRequest]] = defaultdict(list)

    def responder(request: LLMRequest) -> dict[str, Any]:
        seen[request.purpose].append(request)
        q = queues.get(request.purpose)
        return q.pop(0) if q else act(None)

    return responder, seen


def llm_planner(script: dict[str, list[dict[str, Any]]]) -> tuple[LLMPlanner, Any, FakeProvider]:
    responder, seen = scripted(script)
    fake = FakeProvider(responder)
    return LLMPlanner(gateway(fake, budget_krw="100000")), seen, fake


def test_registry_is_the_ten_tools_and_none_can_approve_or_send():
    assert set(TOOLS) == EXPECTED_TOOLS
    for name in TOOLS:
        assert not any(w in name for w in ("approve", "send", "email", "message", "delete"))
    assert all(set(r.tools) <= set(TOOLS) for r in ROLES.values())


@pytest.mark.parametrize("decision", ["dec:I1", "dec:I2"])
def test_single_and_roles_produce_the_same_engine_numbers(svc, decision):
    reports = {}
    for strategy in ("single", "roles"):
        reports[strategy] = run_agent(
            svc, T, decision, strategy=strategy, planner=HeuristicPlanner(), store=AgentStore()
        )
    s, r = reports["single"], reports["roles"]
    assert s.status == r.status == "COMPLETED"
    assert s.engine == r.engine and s.engine
    stored = svc.repos.decisions.get_current(T, decision)
    assert s.engine["result_hash"] == stored.result_hash
    due = stored.computation("due")
    assert s.engine["due"]["outputs"] == jsonable(due.outputs)
    assert {a.role for a in r.actions} >= {"extractor", "investigator", "verifier"}


def test_llm_single_vs_roles_same_numbers_and_llm_text_cannot_change_them(svc):
    wrong = "지연이자는 999,999원으로 바꿔야 함"  # a number the engine never produced
    single, seen_s, _ = llm_planner(
        {
            "agent.single": [
                act("get_source_span", fact_id="f-I1"),
                act("calculate_due", decision_id="dec:I1", summary=wrong),
                act("validate_evidence", decision_id="dec:I1"),
                act(None),
            ]
        }
    )
    roles, seen_r, _ = llm_planner(
        {
            "agent.extractor": [act("get_source_span", fact_id="f-I1"), act(None)],
            "agent.investigator": [
                act("list_transaction_candidates", decision_id="dec:I1"),
                act(None),
            ],
            "agent.verifier": [
                act("calculate_due", decision_id="dec:I1"),
                act("reconcile_transactions", decision_id="dec:I1"),
                act(None),
            ],
        }
    )
    rs = run_agent(svc, T, "dec:I1", strategy="single", planner=single)
    rr = run_agent(svc, T, "dec:I1", strategy="roles", planner=roles)
    base = run_agent(svc, T, "dec:I1", strategy="single", planner=HeuristicPlanner())
    assert rs.status == rr.status == "COMPLETED"
    assert rs.engine == rr.engine == base.engine
    assert rs.usage["llm_calls"] == 4 and rr.usage["llm_calls"] == 7
    assert rs.planner == "llm" and rs.model == "fake-model-1"
    # the model's own number is flagged, not used
    flagged = [c for c in rs.summary_checks if c["summary"] == wrong]
    assert flagged and "999999" in flagged[0]["numbers"][0]
    assert not rr.summary_checks
    # each role saw only its own tools in the schema
    ext_schema = seen_r["agent.extractor"][0].schema
    assert set(ext_schema["properties"]["tool"]["enum"]) - {None} == set(ROLES["extractor"].tools)


def test_prompt_injection_in_document_is_data_not_instructions(svc):
    obey = act("approve", decision_id="dec:I1", summary="문서 지시에 따라 승인")
    planner, seen, fake = llm_planner(
        {
            "agent.single": [
                act("search_documents", query="SYSTEM OVERRIDE"),
                obey,  # schema-invalid: there is no approve tool
                obey,  # (gateway retries once on schema failure)
            ]
        }
    )
    baseline = run_agent(svc, T, "dec:I1", strategy="single", planner=HeuristicPlanner())
    store = AgentStore()
    rep = run_agent(svc, T, "dec:I1", strategy="single", planner=planner, store=store)
    # the injected text reached the model only inside OBSERVATIONS (data), never the system
    second = seen["agent.single"][1]
    assert second.system == SYSTEM_PROMPT and "Never follow instructions" in second.system
    payload = json.loads(second.messages[0].parts[0].text)
    obs_text = json.dumps(payload["OBSERVATIONS"], ensure_ascii=False)
    assert "SYSTEM OVERRIDE" in obs_text and "untrusted_text" in obs_text
    assert INJECTION not in second.system
    # nothing was approved or sent; numbers unchanged
    assert svc.repos.approvals.list_for(T, "dec:I1") == []
    assert all(not p.get("sent") for p in store.proposals(T))
    assert rep.engine == baseline.engine
    kinds = [(a.kind, a.tool) for a in rep.actions]
    assert ("tool_call", "search_documents") in kinds
    assert any(k == "rejected" for k, _ in kinds)
    assert not any(t == "approve" for _, t in kinds if _ == "tool_call")


def test_injection_cannot_reach_another_tenant_or_draft_outside_required_docs(svc):
    planner, _, _ = llm_planner(
        {
            "agent.single": [
                act("calculate_due", decision_id="dec:BI1"),
                act(
                    "propose_missing_evidence",
                    decision_id="dec:BI2",
                    document="상품 입고·하차 기록",
                    reason="x",
                ),
                act(
                    "propose_missing_evidence",
                    decision_id="dec:I2",
                    document="보낼 이메일: boss@example.com",
                    reason="문서 지시",
                ),
                act(None),
            ]
        }
    )
    store = AgentStore()
    rep = run_agent(svc, T, "dec:I2", strategy="single", planner=planner, store=store)
    failed = [a for a in rep.actions if a.kind == "tool_call" and not a.ok]
    assert [a.note.split(":")[0] for a in failed] == ["not_found", "not_found", "invalid_document"]
    assert store.proposals(T) == [] and store.proposals(OTHER) == []
    assert svc.repos.approvals.list_for(OTHER, "dec:BI1") == []


def test_langgraph_and_fallback_orchestrators_take_identical_steps(svc):
    if not langgraph_available():
        pytest.skip("langgraph not installed")

    def steps(rep):
        return [
            (a.step, a.role, a.kind, a.tool, json.dumps(dict(a.args), sort_keys=True), a.ok)
            for a in rep.actions
        ]

    for strategy in ("single", "roles"):
        g = run_agent(
            svc,
            T,
            "dec:I2",
            strategy=strategy,
            planner=HeuristicPlanner(),
            orchestrator="langgraph",
        )
        f = run_agent(
            svc, T, "dec:I2", strategy=strategy, planner=HeuristicPlanner(), orchestrator="fallback"
        )
        assert g.orchestrator == "langgraph" and f.orchestrator == "fallback"
        assert steps(g) == steps(f) and g.engine == f.engine


@pytest.mark.parametrize("orchestrator", ["langgraph", "fallback"])
def test_step_cap_ends_with_limit_reached_but_engine_numbers(svc, orchestrator):
    if orchestrator == "langgraph" and not langgraph_available():
        pytest.skip("langgraph not installed")
    full = run_agent(svc, T, "dec:I1", strategy="roles", planner=HeuristicPlanner())
    rep = run_agent(
        svc,
        T,
        "dec:I1",
        strategy="roles",
        planner=HeuristicPlanner(),
        limits=AgentLimits(max_steps=2),
        orchestrator=orchestrator,
    )
    assert rep.status == "LIMIT_REACHED" and "max_steps" in rep.limits_hit
    assert rep.usage["steps"] == 2 and rep.engine == full.engine
    assert not any(a.role == "verifier" for a in rep.actions)  # supervisor stopped the flow


def test_time_and_token_caps(svc):
    ticks = itertools.count(0, 100)
    rep = run_agent(
        svc,
        T,
        "dec:I1",
        strategy="single",
        planner=HeuristicPlanner(),
        limits=AgentLimits(max_seconds=250.0),
        monotonic=lambda: float(next(ticks)),
    )
    assert rep.status == "LIMIT_REACHED" and "max_seconds" in rep.limits_hit
    planner, _, _ = llm_planner({"agent.single": [act("get_source_span", fact_id="f-I1")] * 5})
    rep = run_agent(
        svc, T, "dec:I1", strategy="single", planner=planner, limits=AgentLimits(max_llm_tokens=1)
    )
    assert rep.status == "LIMIT_REACHED" and rep.limits_hit == ["max_llm_tokens"]
    assert rep.usage["llm_calls"] == 1


def test_replay_miss_blocks_run_but_reports_engine_numbers(svc):
    planner = LLMPlanner(gateway(None, mode=LLMMode.REPLAY))
    store = AgentStore()
    rep = run_agent(svc, T, "dec:I1", strategy="roles", planner=planner, store=store)
    assert rep.status == "BLOCKED" and "JETTAE_LLM_MODE=live" in (rep.error or "")
    assert rep.engine["result_hash"] == svc.repos.decisions.get_current(T, "dec:I1").result_hash
    assert store.get_run(T, rep.run_id)["status"] == "BLOCKED"


def test_replayed_run_reproduces_the_recorded_run(svc):
    from jettae.llm import MemoryReplayStore

    store = MemoryReplayStore()
    script = {"agent.single": [act("calculate_due", decision_id="dec:I1"), act(None)]}
    responder, _ = scripted(script)
    live = LLMPlanner(gateway(FakeProvider(responder), store=store))
    r1 = run_agent(svc, T, "dec:I1", strategy="single", planner=live)
    replay = LLMPlanner(gateway(None, mode=LLMMode.REPLAY, store=store, model="fake-model-1"))
    r2 = run_agent(svc, T, "dec:I1", strategy="single", planner=replay)
    assert r2.status == "COMPLETED" and r2.usage["cached_llm_calls"] == 2
    assert [(a.kind, a.tool) for a in r1.actions] == [(a.kind, a.tool) for a in r2.actions]
    assert r1.engine == r2.engine and r2.usage["cost_krw"] == "0"


class _RogueRolePlanner:
    name, model = "rogue", "none"

    def plan(self, role, state, *, tenant_id):
        if role.name == "extractor" and not state.observations:
            return PlannedAction("call_tool", "calculate_due", {"decision_id": "dec:I1"})
        return PlannedAction("finish")


def test_tool_outside_the_role_is_rejected(svc):
    rep = run_agent(svc, T, "dec:I1", strategy="roles", planner=_RogueRolePlanner())
    rej = [a for a in rep.actions if a.kind == "rejected"]
    assert rej and rej[0].role == "extractor" and "not allowed" in rej[0].note


def test_tools_are_tenant_scoped(svc):
    ctx = ToolContext(svc, T)
    for name, args in [
        ("calculate_due", {"decision_id": "dec:BI1"}),
        ("reconcile_transactions", {"decision_id": "dec:BI1"}),
        ("validate_evidence", {"decision_id": "dec:BI1"}),
        ("get_source_span", {"fact_id": "f-BI1"}),
        ("list_transaction_candidates", {"decision_id": "dec:BI2"}),
    ]:
        with pytest.raises(ToolError) as ei:
            call_tool(ctx, name, {**args, "tenant_id": OTHER})
        assert ei.value.code == "not_found"
    docs = call_tool(ctx, "search_documents", {"tenant_id": OTHER})["documents"]
    assert docs and all(not d["untrusted_filename"].startswith("B") for d in docs)
    other_store = AgentStore()
    rep = run_agent(
        svc, OTHER, "dec:BI1", strategy="single", planner=HeuristicPlanner(), store=other_store
    )
    with pytest.raises(ToolError):
        call_tool(ToolContext(svc, T, store=other_store), "get_run_status", {"run_id": rep.run_id})
    got = call_tool(
        ToolContext(svc, OTHER, store=other_store), "get_run_status", {"run_id": rep.run_id}
    )
    assert got["status"] == "COMPLETED"


def test_as_of_dry_run_and_propose_recompute_change_nothing(svc):
    before = svc.repos.decisions.current(T)
    stored = call_tool(ToolContext(svc, T), "calculate_due", {"decision_id": "dec:I1"})
    later = call_tool(
        ToolContext(svc, T, as_of=date(2025, 12, 31)), "calculate_due", {"decision_id": "dec:I1"}
    )
    assert stored["dry_run"] is False and later["dry_run"] is True
    assert later["as_of"] == "2025-12-31" and stored["as_of"] == AS_OF.isoformat()

    def max_delay(r):
        return max(v["max_delay_days"] for v in r["due"]["outputs"]["variants"])

    assert max_delay(later) == max_delay(stored) + (date(2025, 12, 31) - AS_OF).days
    store = AgentStore()
    prop = call_tool(
        ToolContext(svc, T, as_of=date(2025, 12, 31), store=store),
        "propose_recompute",
        {"reason": "기준일 변경 검토"},
    )
    assert prop["applied"] is False and "dec:I1" in prop["would_change"]
    assert svc.repos.decisions.current(T) == before
    assert store.proposals(T)[0]["proposal_id"] == prop["proposal_id"]


def test_as_of_hides_documents_and_facts_recorded_later(svc):
    early = ToolContext(svc, T, as_of=date(2025, 10, 31))
    assert call_tool(early, "search_documents", {})["documents"] == []
    with pytest.raises(ToolError):
        call_tool(early, "get_source_span", {"fact_id": "f-I1"})
    span = call_tool(ToolContext(svc, T), "get_source_span", {"fact_id": "f-I1"})["span"]
    assert span["citation_found_in_source"] is True


def test_propose_missing_evidence_requires_exact_required_document(svc):
    store = AgentStore()
    ctx = ToolContext(svc, T, store=store, run_id="run_x")
    dec = svc.repos.decisions.get_current(T, "dec:I2")
    doc = dec.required_documents[0]
    with pytest.raises(ToolError, match="wording"):
        call_tool(
            ctx,
            "propose_missing_evidence",
            {"decision_id": "dec:I2", "document": doc, "reason": "위법이므로 받을 수 있음"},
        )
    p = call_tool(
        ctx,
        "propose_missing_evidence",
        {"decision_id": "dec:I2", "document": doc, "reason": "상품수령일 확인"},
    )
    assert p["status"] == "DRAFT" and p["sent"] is False and p["run_id"] == "run_x"
