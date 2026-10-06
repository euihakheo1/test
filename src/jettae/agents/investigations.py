"""Agent investigation of one decision, as started from the web page (worker job
``investigate_decision``).

Modes:

- ``offline``: :class:`HeuristicPlanner` (deterministic, no LLM call, no cost);
- ``local``: a private text-only vLLM server, enabled by the server configuration;
- ``replay``: :class:`LLMPlanner` over a replay-only gateway; a missing recording ends the
  run as ``failed`` (``replay_miss``) instead of calling a model;
- ``live``: :class:`LLMPlanner` over the live gateway, only when the *server* settings allow
  it (:func:`live_capability`); otherwise the investigation is ``refused`` before any
  provider client or budget store is created. A request body cannot enable paid calls.

``single`` and ``roles`` run over the same typed tools (:mod:`jettae.agents.tools`), the same
:class:`AgentLimits` and the same gateway (hence the same budget).

Findings are produced by code from the stored decision and typed tool results
(:func:`build_findings`), never from model text: the planner only chooses which tools to call.
So a model, or an instruction hidden in a document, cannot change an amount, a date or the
list of required documents shown to the user, and the investigation never writes decisions,
approvals or ledger records (tools only read; ``propose_*`` drafts stay in an in-memory store
and are reported, never sent).
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from jettae.agents.flows import AgentLimits, AgentReport, run_agent
from jettae.agents.planners import HeuristicPlanner, LLMPlanner, Planner
from jettae.agents.stores import AgentStore
from jettae.agents.tools import ToolContext, ToolError, call_tool, jsonable
from jettae.app.services import JettaeService
from jettae.domain.models import Decision
from jettae.domain.money import Money
from jettae.domain.status import ReconcileStatus
from jettae.llm.base import LiveCallRefused, LLMError, TokenUsage
from jettae.llm.budget import budget_limit_problem
from jettae.llm.gateway import CallRecord, LLMGateway, LLMMode, gateway_from_env

Strategy = Literal["single", "roles"]
Mode = Literal["offline", "replay", "live", "local"]
STRATEGIES: tuple[str, ...] = ("single", "roles")
MODES: tuple[str, ...] = ("offline", "replay", "live", "local")
GatewayFactory = Callable[[LLMMode], LLMGateway]

# Same caps for both strategies (SPEC §5: same input, tools and budget).
DEFAULT_LIMITS = AgentLimits(max_steps=16, max_tool_calls=16, max_llm_tokens=200_000)
MAX_CITATIONS = 8
REFUSED_KINDS = frozenset({"LiveCallRefused", "BudgetExceeded", "ExternalLLMNotAllowed"})

MISSING_LABELS = {
    "goods_received_date": "상품수령일(입고·하차일)",
    "sales_close_date": "월 판매마감일",
    "trade_type": "거래 형태(직매입·특약매입 등)",
    "direction": "매출·매입 구분",
    "counterparty": "거래처",
}
CONDITION_LABELS = {
    "rollover": "기한 말일이 휴일일 때 다음 영업일로 넘기는지(rollover)",
    "rounding": "원 단위 처리 방식(절사·반올림)",
}
DOC_LABELS = {"invoice": "세금계산서", "settlement_line": "정산 행", "bank_txn": "입금"}
PENDING_KINDS = ("evidence_link", "duplicate_line")
CONFLICT_ITEMS = ("agreement_conflict", "trade_type_conflict", "contract_term_conflict")
HANDLED = frozenset(
    {*PENDING_KINDS, "invoice_direction", "evidence_amount_conflict", "allocation", *CONFLICT_ITEMS}
)


# ----------------------------------------------------------------------------- capability
@dataclass(frozen=True)
class LiveCapability:
    enabled: bool
    reason: str = ""


def live_capability(env: Mapping[str, str] | None = None) -> LiveCapability:
    """Whether this server may make paid model calls for investigations.

    Needs ``JETTAE_LLM_MODE=live`` and ``JETTAE_LLM_BUDGET_KRW > 0`` (AGENTS.md rule 5); with
    ``JETTAE_ENV=prod`` also a shared PostgreSQL budget database (``JETTAE_LLM_BUDGET_DB``),
    because API and worker processes on several machines must draw from one limit (the same
    rule as the startup gate ``jettae.config.environment_problems``). Reasons never contain
    setting values."""
    e: Mapping[str, str] = os.environ if env is None else env
    if (e.get("JETTAE_LLM_MODE") or "offline").strip().lower() != "live":
        return LiveCapability(False, "서버 설정에서 실제 LLM 호출이 꺼져 있습니다.")
    try:
        budget = Decimal((e.get("JETTAE_LLM_BUDGET_KRW") or "0").strip() or "0")
    except InvalidOperation:
        return LiveCapability(False, "JETTAE_LLM_BUDGET_KRW 값이 숫자가 아닙니다.")
    if not budget.is_finite() or budget <= 0:
        return LiveCapability(False, "서버의 LLM 예산(JETTAE_LLM_BUDGET_KRW)이 0원입니다.")
    if budget_limit_problem(budget):
        return LiveCapability(
            False, "서버의 LLM 예산(JETTAE_LLM_BUDGET_KRW)이 허용 범위를 넘습니다."
        )
    budget_db = (e.get("JETTAE_LLM_BUDGET_DB") or "").strip()
    scheme = budget_db.split(":", 1)[0].split("+", 1)[0]
    if (e.get("JETTAE_ENV") or "dev").strip().lower() == "prod" and scheme not in (
        "postgresql",
        "postgres",
    ):
        return LiveCapability(
            False, "운영 환경에서는 공유 예산 DB(JETTAE_LLM_BUDGET_DB, PostgreSQL)가 필요합니다."
        )
    return LiveCapability(True)


def capabilities(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    live = live_capability(env)
    modes = ["offline", "replay", *(["live"] if live.enabled else [])]
    out: dict[str, Any] = {"modes_enabled": modes, "live_enabled": live.enabled}
    if not live.enabled:
        out["live_disabled_reason"] = live.reason
    e = os.environ if env is None else env
    if (e.get("JETTAE_LLM_MODE") or "offline").strip().lower() == "local":
        from jettae.llm.vllm import local_configuration_problem

        if local_configuration_problem(e) is None:
            modes.append("local")
            out["local_enabled"] = True
    return out


def default_gateway_factory(mode: LLMMode) -> LLMGateway:
    return gateway_from_env(mode=mode)


# ----------------------------------------------------------------------------- outcome
@dataclass(frozen=True)
class InvestigationOutcome:
    status: str  # succeeded | failed | refused
    findings: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    @classmethod
    def stopped(cls, status: str, code: str, message: str) -> InvestigationOutcome:
        return cls(status, [], empty_usage(), None, {"code": code, "message": message[:1000]})


def empty_usage() -> dict[str, Any]:
    return {
        "steps": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_krw": 0,
        "cost_krw_exact": "0",
        "llm_calls": 0,
        "cached_llm_calls": 0,
    }


def usage_of(report: AgentReport, calls: Sequence[CallRecord] | None = None) -> dict[str, Any]:
    """Usage in the API shape. Steps and tool calls come from the run; LLM calls, tokens and
    cost come from the gateway's call records when there are any (``calls``), because those
    also include calls whose output was rejected (schema retries): they were paid for even
    though the planner produced no action from them.

    ``cost_krw`` is whole won rounded UP (a display value that never understates spending);
    ``cost_krw_exact`` keeps the Decimal as text. Unknown cost (a model without a price) stays
    ``None`` rather than being shown as 0."""
    u = report.usage
    exact: Decimal | None
    if calls is None:
        tokens = u.get("tokens") or {}
        raw = u.get("cost_krw")
        exact = Decimal(str(raw)) if raw is not None else None
        n_calls, n_cached = int(u.get("llm_calls", 0)), int(u.get("cached_llm_calls", 0))
        tin, tout = int(tokens.get("input_tokens", 0)), int(tokens.get("output_tokens", 0))
    else:
        total = TokenUsage()
        exact = Decimal(0)
        for c in calls:
            total = total + c.usage
            exact = None if exact is None or c.cost_krw is None else exact + c.cost_krw
        n_calls, n_cached = len(calls), sum(1 for c in calls if c.from_cache)
        tin, tout = total.input_tokens, total.output_tokens
    return {
        "steps": int(u.get("steps", 0)),
        "tool_calls": int(u.get("tool_calls", 0)),
        "input_tokens": tin,
        "output_tokens": tout,
        "cost_krw": None if exact is None else int(math.ceil(exact)),
        "cost_krw_exact": None if exact is None else str(exact),
        "llm_calls": n_calls,
        "cached_llm_calls": n_cached,
    }


def report_summary(report: AgentReport) -> dict[str, Any]:
    """What the page may show about the run. Model-written action summaries are left out:
    findings are the code-made result, and model text is not repeated as if it were one."""
    return {
        "run_id": report.run_id,
        "planner": report.planner,
        "model": report.model,
        "orchestrator": report.orchestrator,
        "agent_status": report.status,
        "limits": report.limits,
        "limits_hit": list(report.limits_hit),
        "engine_result_hash": report.engine.get("result_hash"),
        "actions": [
            {
                "step": a.step,
                "role": a.role,
                "kind": a.kind,
                "tool": a.tool,
                "ok": a.ok,
                "note": a.note[:300],
            }
            for a in report.actions
        ],
        "proposals": [
            {
                k: p.get(k)
                for k in ("proposal_id", "type", "document", "status", "sent", "would_change")
                if k in p
            }
            for p in report.proposals
        ],
        "summary_check_failures": len(report.summary_checks),
    }


# ----------------------------------------------------------------------------- run
def investigate(
    service: JettaeService,
    tenant_id: str,
    decision_id: str,
    *,
    strategy: str,
    mode: str,
    gateway_factory: GatewayFactory | None = None,
    env: Mapping[str, str] | None = None,
    limits: AgentLimits | None = None,
    actor: str = "agent",
    orchestrator: Literal["auto", "langgraph", "fallback"] = "auto",
) -> InvestigationOutcome:
    """Run one investigation of ``decision_id`` for the server-resolved ``tenant_id``."""
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    planner: Planner
    gw: LLMGateway | None = None
    first_call = 0
    if mode == "offline":
        planner = HeuristicPlanner()
    else:
        if mode == "local":
            from jettae.llm.vllm import local_configuration_problem

            problem = local_configuration_problem(os.environ if env is None else env)
            if problem:
                return InvestigationOutcome.stopped("refused", "local_disabled", problem)
        if mode == "live":
            cap = live_capability(env)
            if not cap.enabled:
                return InvestigationOutcome.stopped("refused", "live_disabled", cap.reason)
        factory = gateway_factory or default_gateway_factory
        try:
            gw = factory(LLMMode(mode))
        except LiveCallRefused as e:
            return InvestigationOutcome.stopped("refused", "live_refused", str(e))
        except LLMError as e:
            return InvestigationOutcome.stopped("failed", "llm_config", str(e))
        planner = LLMPlanner(gw)
        first_call = len(gw.records)
    store = AgentStore()
    report = run_agent(
        service,
        tenant_id,
        decision_id,
        strategy="single" if strategy == "single" else "roles",
        planner=planner,
        limits=limits or DEFAULT_LIMITS,
        store=store,
        orchestrator=orchestrator,
        actor=actor,
    )
    usage = usage_of(report, None if gw is None else gw.records[first_call:])
    summary = report_summary(report)
    kind = report.error_kind or ""
    if report.status == "BLOCKED":
        if kind in REFUSED_KINDS:
            err = {"code": "live_refused", "message": report.error or ""}
            return InvestigationOutcome("refused", [], usage, summary, err)
        code = "replay_miss" if kind == "ReplayMiss" else "blocked"
        return InvestigationOutcome("failed", [], usage, summary, _err(code, report.error))
    if report.status == "FAILED":
        code = "decision_not_found" if kind == "ToolError:not_found" else "agent_failed"
        return InvestigationOutcome("failed", [], usage, summary, _err(code, report.error))
    ctx = ToolContext(service, tenant_id, actor=actor, run_id=report.run_id, store=store)
    try:
        decision = ctx.decision(decision_id)
    except ToolError as e:
        err = _err("decision_not_found", str(e))
        return InvestigationOutcome("failed", [], usage, summary, err)
    findings = build_findings(ctx, decision)
    return InvestigationOutcome("succeeded", findings, usage, summary, None)


def _err(code: str, message: str | None) -> dict[str, Any]:
    return {"code": code, "message": (message or "")[:1000]}


# ----------------------------------------------------------------------------- findings
def _won(v: Any) -> int | None:
    if isinstance(v, Money):
        return v.amount
    if isinstance(v, Mapping) and "amount" in v:
        return int(v["amount"])
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return None


def _fmt(v: Any) -> str:
    w = _won(v)
    return "금액 미상" if w is None else f"{w:,}원"


def _finding(
    kind: str,
    message: str,
    required: Iterable[str] = (),
    citations: Sequence[Mapping[str, Any]] = (),
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": kind,
        "message": message,
        "required_documents": list(required),
        "citations": [dict(c) for c in citations],
    }
    if details:
        out["details"] = jsonable(dict(details))
    return out


def _citations(ctx: ToolContext, fact_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Source spans through the typed ``get_source_span`` tool (tenant-scoped; also reports
    whether the excerpt is found in the stored source text)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fid in fact_ids:
        if fid in seen or len(out) >= MAX_CITATIONS:
            continue
        seen.add(fid)
        try:
            res = call_tool(ctx, "get_source_span", {"fact_id": fid})
        except ToolError:
            continue
        span = res.get("span")
        if not span:
            continue
        out.append(
            {
                "doc_version_id": span["doc_version_id"],
                "locator": span["locator"],
                "excerpt": span["untrusted_excerpt"],
                "fact_id": fid,
                "found_in_source": span["citation_found_in_source"],
            }
        )
    return out


def _record_facts(ctx: ToolContext, ids: Iterable[str]) -> list[str]:
    snap = ctx.snapshot()
    txns = {t.id: t for t in snap.bank_txns}
    out: list[str] = []
    for rid in ids:
        rec = snap.documents.get(rid) or txns.get(rid)
        if rec is not None:
            out.extend(rec.facts)
    return out


def _pick(required: Sequence[str], *hints: str) -> list[str]:
    return [d for d in required if any(h in d for h in hints)]


def build_findings(ctx: ToolContext, dec: Decision) -> list[dict[str, Any]]:
    """Deterministic findings for one decision (wording: differences, required documents,
    unconfirmed conditions, sources; no legal conclusions).

    - INSUFFICIENT_EVIDENCE: what is missing, the required documents, the cited facts;
    - AMBIGUOUS document link: candidate settlement lines and the same_sale / separate_sale
      confirmation; AMBIGUOUS allocation: the candidate payments;
    - CONFLICT: both amounts (or the conflicting conditions) with their source spans;
    - MATCHED without required documents: says first that nothing is missing (unconfirmed
      calculation conditions such as rollover follow as their own finding).
    Every required document is quoted verbatim from the decision."""
    required = list(dec.required_documents)
    used_docs: set[str] = set()
    out: list[dict[str, Any]] = []
    ev = dec.computation("evidence")
    evo: Mapping[str, Any] = ev.outputs if ev is not None else {}
    conf = evo.get("confirmation_required") or None
    base_cites = _citations(ctx, dec.facts_used)

    def take(docs: Iterable[str]) -> list[str]:
        docs = [d for d in docs if d not in used_docs]
        used_docs.update(docs)
        return docs

    # --- document link waiting for the user's confirmation (AMBIGUOUS)
    if isinstance(conf, Mapping) and conf.get("kind") in PENDING_KINDS:
        out.append(_confirmation(ctx, dec, conf, evo, take(required)))

    # --- linked documents with different amounts (CONFLICT)
    if "evidence_amount_conflict" in dec.unresolved:
        out.append(_amount_conflict(ctx, dec, evo, take(_pick(required, "금액"))))

    # --- conflicting agreement / trade conditions (CONFLICT)
    conflicts = [u for u in dec.unresolved if u in CONFLICT_ITEMS]
    if conflicts:
        notes = [a for a in dec.assumptions if "약정" in a or "불일치" in a or "조건" in a]
        ag = call_tool(ctx, "get_agreement_conditions", {"decision_id": dec.id})
        ag_facts = [f for a in ag.get("agreements", []) for f in a.get("facts", [])]
        if conflicts == ["contract_term_conflict"]:
            # the statutory due date is still computed; only the contractual one is open
            msg = "약정서마다 지급 일수가 달라 약정 기한을 하나로 정하지 않았습니다"
        else:
            msg = "적용할 약정·거래 조건이 서로 달라 지급기한을 계산하지 않았습니다"
        msg += f": {'; '.join(notes)}." if notes else "."
        out.append(
            _finding(
                "condition_conflict",
                msg,
                take(_pick(required, "약정", "계약")),
                base_cites + _citations(ctx, ag_facts),
                {"unresolved": conflicts},
            )
        )

    # --- payment allocation cannot be decided (AMBIGUOUS)
    if "allocation" in dec.unresolved:
        out.append(_allocation(ctx, dec, take(_pick(required, "송금", "지급 통지"))))

    # --- base date / direction missing (INSUFFICIENT_EVIDENCE)
    if dec.status is ReconcileStatus.INSUFFICIENT_EVIDENCE or (
        dec.missing and not any(f["kind"] == "confirmation_required" for f in out)
    ):
        labels = [MISSING_LABELS.get(m, m) for m in dec.missing]
        what = ", ".join(labels) if labels else "계산 기준 정보"
        msg = f"지급기한 계산에 필요한 정보({what})가 없어 지급기한·지연일수를 계산하지 않았습니다."
        if "goods_received_date" in dec.missing:
            msg += " 세금계산서 작성일로 대신 계산하지 않습니다."
        docs = take(required)
        if docs:
            msg += " 아래 서류로 이 정보를 확인할 수 있습니다."
        out.append(
            _finding("required_documents", msg, docs, base_cites, {"missing": list(dec.missing)})
        )

    # --- open amount (PARTIAL / UNMATCHED)
    recon = dec.computation("recon")
    if recon is not None and dec.status in (ReconcileStatus.PARTIAL, ReconcileStatus.UNMATCHED):
        open_amt = recon.outputs.get("open")
        if dec.status is ReconcileStatus.UNMATCHED:
            msg = f"이 채권에 배분된 입금이 없습니다. 남은 금액: {_fmt(open_amt)}(엔진 대사 결과)."
        else:
            msg = f"입금을 배분한 뒤 남은 금액: {_fmt(open_amt)}(엔진 대사 결과)."
        out.append(_finding("difference", msg, [], base_cites, {"open": open_amt}))

    # --- required documents not covered above
    rest = take(required)
    if rest:
        out.append(
            _finding(
                "required_documents",
                "이 결과를 확인하는 데 아래 서류가 필요합니다.",
                rest,
                base_cites,
            )
        )

    # --- other unconfirmed conditions (e.g. rollover): both variants are shown
    others = [u for u in dec.unresolved if u not in HANDLED]
    if others:
        labels = [CONDITION_LABELS.get(u, u) for u in others]
        out.append(
            _finding(
                "condition_unconfirmed",
                "확인이 필요한 조건이 있어 계산 방식별 결과를 함께 표시합니다: "
                + "; ".join(labels)
                + ".",
                [],
                base_cites,
                {"unresolved": others},
            )
        )

    if dec.status is ReconcileStatus.MATCHED and not required:
        # An unconfirmed calculation condition (e.g. rollover) is not a missing document:
        # the page still states first that nothing is missing.
        msg = "엔진 대사 결과 입금과 금액이 맞고, 이 결과에 추가로 필요한 서류는 없습니다."
        out.insert(0, _finding("nothing_missing", msg, [], base_cites))
    elif not out:
        msg = "이 결과에 추가로 필요한 서류나 확인할 조건이 없습니다."
        out.append(_finding("nothing_missing", msg, [], base_cites))
    return out


def _confirmation(
    ctx: ToolContext,
    dec: Decision,
    conf: Mapping[str, Any],
    evo: Mapping[str, Any],
    docs: list[str],
) -> dict[str, Any]:
    kind = str(conf["kind"])
    basis_entity = str(evo.get("basis", ""))
    basis_id = str(conf.get("document_id") or evo.get("basis_id") or "")
    candidates = [c for c in evo.get("candidates", []) if isinstance(c, Mapping)]
    if not candidates:  # older stored decisions: ids only
        candidates = [
            {"entity": "settlement_line", "id": cid, "amount": None}
            for cid in conf.get("candidate_settlement_lines", [])
        ]
    label = DOC_LABELS.get(basis_entity, "문서")
    cand_text = ", ".join(f"[{c['id']}] {_fmt(c.get('amount'))}" for c in candidates) or "없음"
    if kind == "duplicate_line":
        msg = (
            f"이 정산 행 [{basis_id}] {_fmt(evo.get('basis_amount'))}이 먼저 올린 다른 문서의 "
            f"정산 행({cand_text})과 같은 거래(재발행)인지 확인이 필요합니다."
        )
    else:
        msg = (
            f"이 {label} [{basis_id}] {_fmt(evo.get('basis_amount'))}이 다음 정산 행과 같은 "
            f"거래인지 확인이 필요합니다: {cand_text}."
        )
    msg += (
        " 같은 거래(same_sale)로 확인하면 금액을 더하지 않는 보조 근거로 연결되고, 별개 거래"
        "(separate_sale)로 확인하면 따로 집계됩니다. 확인 전에는 합계와 입금 배분에서 빠집니다."
    )
    fact_ids = [*dec.facts_used, *_record_facts(ctx, [c["id"] for c in candidates])]
    return _finding(
        "confirmation_required",
        msg,
        docs,
        _citations(ctx, fact_ids),
        {
            "confirmation": kind,
            "document_id": basis_id,
            "choices": list(conf.get("choices", [])),
            "candidates": [
                {"entity": c.get("entity"), "id": c["id"], "amount": c.get("amount")}
                for c in candidates
            ],
        },
    )


def _amount_conflict(
    ctx: ToolContext, dec: Decision, evo: Mapping[str, Any], docs: list[str]
) -> dict[str, Any]:
    documents = [d for d in evo.get("documents", []) if isinstance(d, Mapping)]
    basis = next((d for d in documents if d.get("role") == "basis"), None)
    base_amt = _won(basis.get("amount")) if basis else _won(evo.get("basis_amount"))
    differing = [d for d in documents if d is not basis and _won(d.get("amount")) != base_amt]
    shown = [d for d in (basis, *differing) if d is not None]
    parts = [
        f"{DOC_LABELS.get(str(d.get('entity')), '문서')} [{d.get('id')}] {_fmt(d.get('amount'))}"
        for d in shown
    ]
    msg = (
        "같은 거래로 연결된 문서의 금액이 서로 다릅니다: "
        + " / ".join(parts)
        + ". 어느 금액이 맞는지 정할 수 없어 지급기한·지연이자를 계산하지 않았습니다."
    )
    cites = _citations(ctx, _record_facts(ctx, [str(d.get("id")) for d in shown]))
    return _finding(
        "amount_conflict",
        msg,
        docs,
        cites,
        {
            "amounts": [
                {"entity": d.get("entity"), "id": d.get("id"), "amount": d.get("amount")}
                for d in shown
            ]
        },
    )


def _allocation(ctx: ToolContext, dec: Decision, docs: list[str]) -> dict[str, Any]:
    recon = dec.computation("recon")
    ids = [str(t) for t in (recon.outputs.get("candidates") or ())] if recon else []
    try:
        listed = call_tool(ctx, "list_transaction_candidates", {"decision_id": dec.id})
        by_id = {c["txn_id"]: c for c in listed.get("candidates", [])}
    except ToolError:
        by_id = {}
    parts = []
    for tid in ids:
        c = by_id.get(tid)
        if c is None:
            parts.append(f"[{tid}]")
        else:
            parts.append(f"[{tid}] {c['booked_date']} {_fmt(c['amount'])}")
    msg = "이 채권에 배분할 입금을 하나로 정할 수 없어 지연일수·지연이자를 계산하지 않았습니다."
    if parts:
        msg += f" 입금 후보: {', '.join(parts)}."
    return _finding(
        "allocation_ambiguous",
        msg,
        docs,
        _citations(ctx, [*dec.facts_used, *_record_facts(ctx, ids)]),
        {"candidate_payments": ids},
    )
