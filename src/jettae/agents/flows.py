"""Agent flows: one agent (``single``) or role split (``roles``: extractor -> investigator ->
verifier under a code supervisor). Both use the SAME typed tools, planner and limits.

Orchestration runs on LangGraph when it is installed (extra ``agents``); otherwise a minimal
deterministic orchestrator with the same node functions and routing is used. Either way:

- caps: total steps, tool calls, LLM tokens and wall time (plus a per-role step cap) are
  checked before every step; hitting one ends the loop with status ``LIMIT_REACHED``;
- the final numbers are produced by code after the loop (``finalize`` calls the engine
  tools), so the LLM never sets or changes an amount, date or delay;
- each step is recorded as a structured action summary (role, tool, args, outcome, result
  hash, a one-sentence summary); no chain-of-thought is requested or stored;
- tools cannot approve or send; drafts are stored for the user.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, TypedDict

from jettae.agents.planners import (
    ROLES,
    Observation,
    PlannedAction,
    Planner,
    PlannerState,
    RoleSpec,
)
from jettae.agents.stores import AgentStore
from jettae.agents.tools import ToolContext, ToolError, call_tool, jsonable
from jettae.app.services import JettaeService
from jettae.domain.dates import utc_now
from jettae.domain.hashing import content_hash
from jettae.llm.base import LiveCallRefused, LLMError, ReplayMiss, SchemaValidationError, TokenUsage
from jettae.verify.checks import check_numbers, check_wording, collect_engine_values

Strategy = Literal["single", "roles"]
ROLE_ORDER = ("extractor", "investigator", "verifier")


@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 16  # planner steps over all roles
    max_tool_calls: int = 16
    max_llm_tokens: int = 200_000
    max_seconds: float = 300.0

    def to_json(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_llm_tokens": self.max_llm_tokens,
            "max_seconds": str(self.max_seconds),
        }


@dataclass(frozen=True)
class ActionSummary:
    step: int
    role: str
    kind: str  # tool_call | finish | rejected | finalize | context
    tool: str | None
    args: Mapping[str, Any]
    ok: bool
    result_hash: str | None
    summary: str
    note: str = ""
    llm_run_id: str | None = None
    from_cache: bool = False


@dataclass
class AgentReport:
    run_id: str
    strategy: str
    orchestrator: str
    planner: str
    model: str
    decision_id: str
    status: str  # COMPLETED | LIMIT_REACHED | BLOCKED | FAILED
    as_of: str | None
    engine: dict[str, Any]
    validation: dict[str, Any]
    proposals: list[dict[str, Any]]
    actions: list[ActionSummary]
    summary_checks: list[dict[str, Any]]
    usage: dict[str, Any]
    limits: dict[str, Any]
    limits_hit: list[str]
    started_at: str
    finished_at: str
    error: str | None = None
    # exception class that stopped the run (e.g. ReplayMiss, LiveCallRefused, BudgetExceeded):
    # callers map it to their own states without parsing the message
    error_kind: str | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["actions"] = [jsonable(asdict(a)) for a in self.actions]
        return jsonable(d)


def langgraph_available() -> bool:
    try:
        import langgraph.graph  # noqa: F401
    except ImportError:
        return False
    return True


class _Stop(Exception):
    """Ends the run early (BLOCKED: replay miss / live call refused; FAILED: other)."""

    def __init__(self, status: str, message: str, kind: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind


@dataclass
class _Run:
    ctx: ToolContext
    planner: Planner
    limits: AgentLimits
    decision_id: str
    monotonic: Callable[[], float]
    head: dict[str, Any] = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)
    actions: list[ActionSummary] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_krw: Decimal | None = Decimal(0)
    llm_calls: int = 0
    cached_calls: int = 0
    steps: int = 0
    tool_calls: int = 0
    role_steps: dict[str, int] = field(default_factory=dict)
    limits_hit: list[str] = field(default_factory=list)
    started: float = 0.0
    summaries: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ caps
    def _cap(self, role: RoleSpec) -> str | None:
        if self.steps >= self.limits.max_steps:
            return "max_steps"
        if self.tool_calls >= self.limits.max_tool_calls:
            return "max_tool_calls"
        if self.usage.total >= self.limits.max_llm_tokens:
            return "max_llm_tokens"
        if self.monotonic() - self.started >= self.limits.max_seconds:
            return "max_seconds"
        if self.role_steps.get(role.name, 0) >= role.max_steps:
            return f"role_steps:{role.name}"
        return None

    def _record(self, a: ActionSummary) -> None:
        self.actions.append(a)

    # ------------------------------------------------------------------ one step
    def step(self, role: RoleSpec) -> bool:
        """One planner step of ``role``. Returns True when the role wants another step."""
        cap = self._cap(role)
        if cap is not None:
            if cap not in self.limits_hit:
                self.limits_hit.append(cap)
            return False
        self.steps += 1
        self.role_steps[role.name] = self.role_steps.get(role.name, 0) + 1
        state = PlannerState(
            self.decision_id,
            self.head,
            self.ctx.as_of,
            tuple(self.observations),
            role.max_steps - self.role_steps[role.name] + 1,
        )
        try:
            act: PlannedAction = self.planner.plan(role, state, tenant_id=self.ctx.tenant_id)
        except SchemaValidationError as e:
            self._record(
                ActionSummary(
                    self.steps,
                    role.name,
                    "rejected",
                    None,
                    {},
                    False,
                    None,
                    "",
                    note=f"planner output rejected: {e}",
                )
            )
            return False
        except (ReplayMiss, LiveCallRefused) as e:
            raise _Stop("BLOCKED", str(e), type(e).__name__) from e
        except LLMError as e:
            raise _Stop("FAILED", str(e), type(e).__name__) from e
        self.usage = self.usage + act.usage
        if act.llm_run_id is not None:
            self.llm_calls += 1
            self.cached_calls += int(act.from_cache)
        self.cost_krw = (
            None if self.cost_krw is None or act.cost_krw is None else self.cost_krw + act.cost_krw
        )
        if act.summary:
            self.summaries.append(act.summary)
        if act.action == "finish":
            self._record(
                ActionSummary(
                    self.steps,
                    role.name,
                    "finish",
                    None,
                    {},
                    True,
                    None,
                    act.summary,
                    llm_run_id=act.llm_run_id,
                    from_cache=act.from_cache,
                )
            )
            return False
        tool = act.tool or ""
        if tool not in role.tools:
            self._record(
                ActionSummary(
                    self.steps,
                    role.name,
                    "rejected",
                    tool,
                    dict(act.args),
                    False,
                    None,
                    act.summary,
                    note=f"tool {tool!r} is not allowed for {role.name}",
                    llm_run_id=act.llm_run_id,
                    from_cache=act.from_cache,
                )
            )
            self.observations.append(
                Observation(
                    self.steps,
                    role.name,
                    tool,
                    dict(act.args),
                    False,
                    {"error": "tool not allowed"},
                )
            )
            return True
        self.tool_calls += 1
        try:
            result = call_tool(self.ctx, tool, act.args)
            ok, note = True, ""
        except ToolError as e:
            result, ok, note = {"error": e.code, "message": str(e)}, False, f"{e.code}: {e}"
        self.observations.append(
            Observation(self.steps, role.name, tool, dict(act.args), ok, result)
        )
        self._record(
            ActionSummary(
                self.steps,
                role.name,
                "tool_call",
                tool,
                dict(act.args),
                ok,
                content_hash(jsonable(result)),
                act.summary,
                note,
                act.llm_run_id,
                act.from_cache,
            )
        )
        return True

    # ------------------------------------------------------------------ code-only calls
    def code_call(self, kind: str, tool: str) -> dict[str, Any]:
        result = call_tool(self.ctx, tool, {"decision_id": self.decision_id})
        self._record(
            ActionSummary(
                self.steps,
                "supervisor",
                kind,
                tool,
                {"decision_id": self.decision_id},
                True,
                content_hash(jsonable(result)),
                "",
            )
        )
        return result


# ----------------------------------------------------------------------------- orchestration
class _GraphState(TypedDict):
    phase: str
    more: bool


def _route_roles(run: _Run, queue: list[str]) -> str:
    """Supervisor: next role in order, skipping none; stops early when a global cap hit."""
    global_caps = {"max_steps", "max_tool_calls", "max_llm_tokens", "max_seconds"}
    if global_caps & set(run.limits_hit):
        return "finalize"
    return queue.pop(0) if queue else "finalize"


def _run_fallback(run: _Run, strategy: Strategy) -> None:
    if strategy == "single":
        while run.step(ROLES["single"]):
            pass
        return
    queue = list(ROLE_ORDER)
    while True:
        nxt = _route_roles(run, queue)
        if nxt == "finalize":
            return
        while run.step(ROLES[nxt]):
            pass


def _run_langgraph(run: _Run, strategy: Strategy) -> None:
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(_GraphState)
    recursion = run.limits.max_steps * 3 + 20
    if strategy == "single":
        g.add_node("agent", lambda s: {"more": run.step(ROLES["single"]), "phase": "agent"})
        g.add_node("finalize", lambda s: {"phase": "finalize", "more": False})
        g.add_edge(START, "agent")
        g.add_conditional_edges(
            "agent",
            lambda s: "agent" if s["more"] else "finalize",
            {"agent": "agent", "finalize": "finalize"},
        )
        g.add_edge("finalize", END)
    else:
        queue = list(ROLE_ORDER)
        g.add_node("supervisor", lambda s: {"phase": _route_roles(run, queue), "more": False})
        for name in ROLE_ORDER:
            g.add_node(name, lambda s, name=name: {"more": run.step(ROLES[name]), "phase": name})
            g.add_conditional_edges(
                name,
                lambda s, name=name: name if s["more"] else "supervisor",
                {name: name, "supervisor": "supervisor"},
            )
        g.add_node("finalize", lambda s: {"phase": "finalize", "more": False})
        g.add_edge(START, "supervisor")
        g.add_conditional_edges(
            "supervisor",
            lambda s: s["phase"],
            {**{n: n for n in ROLE_ORDER}, "finalize": "finalize"},
        )
        g.add_edge("finalize", END)
    g.compile().invoke({"phase": "start", "more": False}, config={"recursion_limit": recursion})


# ----------------------------------------------------------------------------- entry point
def run_agent(
    service: JettaeService,
    tenant_id: str,
    decision_id: str,
    *,
    strategy: Strategy,
    planner: Planner,
    limits: AgentLimits | None = None,
    as_of: date | None = None,
    store: AgentStore | None = None,
    orchestrator: Literal["auto", "langgraph", "fallback"] = "auto",
    actor: str = "agent",
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = utc_now,
) -> AgentReport:
    """Run one agent flow for ``decision_id`` of ``tenant_id`` (server-resolved)."""
    if strategy not in ("single", "roles"):
        raise ValueError(f"unknown strategy {strategy!r}")
    lim = limits or AgentLimits()
    st = store or AgentStore()
    run_id = f"run_{uuid.uuid4().hex[:20]}"
    ctx = ToolContext(service, tenant_id, as_of=as_of, actor=actor, run_id=run_id, store=st)
    use_graph = orchestrator == "langgraph" or (orchestrator == "auto" and langgraph_available())
    started_at = now().isoformat()
    st.save_run(
        tenant_id,
        run_id,
        {
            "run_id": run_id,
            "status": "RUNNING",
            "strategy": strategy,
            "decision_id": decision_id,
            "started_at": started_at,
        },
    )
    run = _Run(ctx, planner, lim, decision_id, monotonic, started=monotonic())
    status, error, error_kind = "COMPLETED", None, None
    engine: dict[str, Any] = {}
    validation: dict[str, Any] = {}
    try:
        try:
            recon = run.code_call("context", "reconcile_transactions")
        except ToolError as e:
            raise _Stop("FAILED", str(e), f"ToolError:{e.code}") from e
        run.head = {
            k: recon[k]
            for k in (
                "decision_id",
                "subject_id",
                "status",
                "missing",
                "required_documents",
                "unresolved",
                "facts_used",
            )
        }
        (_run_langgraph if use_graph else _run_fallback)(run, strategy)
        if run.limits_hit and any(not h.startswith("role_steps:") for h in run.limits_hit):
            status = "LIMIT_REACHED"
    except _Stop as e:
        status, error, error_kind = e.status, str(e), e.kind
    # finalize: numbers and checks from the engine, independent of what the planner did
    try:
        due = run.code_call("finalize", "calculate_due")
        rec = run.code_call("finalize", "reconcile_transactions")
        validation = run.code_call("finalize", "validate_evidence")
        engine = {
            "decision_id": rec["decision_id"],
            "subject_id": rec["subject_id"],
            "status": rec["status"],
            "result_hash": rec["result_hash"],
            "missing": rec["missing"],
            "required_documents": rec["required_documents"],
            "unresolved": rec["unresolved"],
            "recon": rec["recon"],
            "allocations": rec["allocations"],
            "due": due["due"],
            "due_as_of": due["as_of"],
            "due_dry_run": due["dry_run"],
        }
    except ToolError as e:
        status, error = "FAILED", error or str(e)
    summary_checks = _check_summaries(run.summaries, engine)
    proposals = st.proposals(tenant_id, run_id)
    finished_at = now().isoformat()
    report = AgentReport(
        run_id=run_id,
        strategy=strategy,
        orchestrator="langgraph" if use_graph else "fallback",
        planner=planner.name,
        model=planner.model,
        decision_id=decision_id,
        status=status,
        as_of=as_of.isoformat() if as_of else None,
        engine=engine,
        validation={k: validation.get(k) for k in ("review_status", "verified", "checks")},
        proposals=proposals,
        actions=run.actions,
        summary_checks=summary_checks,
        usage={
            "steps": run.steps,
            "tool_calls": run.tool_calls,
            "llm_calls": run.llm_calls,
            "cached_llm_calls": run.cached_calls,
            "tokens": run.usage.to_json(),
            "cost_krw": None if run.cost_krw is None else str(run.cost_krw),
        },
        limits=lim.to_json(),
        limits_hit=list(run.limits_hit),
        started_at=started_at,
        finished_at=finished_at,
        error=error,
        error_kind=error_kind,
    )
    st.save_run(tenant_id, run_id, {**report.to_json(), "status": status})
    return report


def _check_summaries(summaries: list[str], engine: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Action summaries are model text: they must not use judgement wording and must not
    contain numbers/dates that the engine did not produce (identifiers in [brackets] and
    small ordinals <= 31 are ignored)."""
    nums: set[Decimal] = set()
    dates: set[date] = set()
    collect_engine_values(_engine_values(engine), nums, dates)
    out = []
    for s in summaries:
        w = check_wording(s)
        n = check_numbers(s, nums, dates, ignore_small=31)
        if not (w.passed and n.passed):
            out.append({"summary": s, "wording": list(w.details), "numbers": list(n.details)})
    return out


_ID_KEYS = frozenset({"result_hash", "decision_id", "subject_id", "source_urls", "source"})


def _engine_values(engine: Any) -> Any:
    """Engine JSON -> values usable by collect_engine_values (amount dicts -> int)."""
    if isinstance(engine, Mapping):
        if set(engine) == {"amount", "currency"}:
            return engine["amount"]
        return {k: _engine_values(v) for k, v in engine.items() if k not in _ID_KEYS}
    if isinstance(engine, list):
        return [_engine_values(v) for v in engine]
    if isinstance(engine, str):
        try:
            return date.fromisoformat(engine)
        except ValueError:
            return engine
    return engine
