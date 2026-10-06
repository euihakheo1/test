"""Agent CLI; for example ``jettae agent run --decision <id> --strategy single --mode local``.

The tenant comes from the API token (``--token`` or ``JETTAE_API_TOKEN``); the database from
``JETTAE_DATABASE_URL``. ``--planner heuristic`` runs the deterministic planner without any
LLM (offline demo / baseline). Exit codes: 0 completed (or limit reached), 2 blocked (replay
miss, live call refused, auth), 3 failed.

Registration: the root CLI (``jettae.cli._SUBAPPS``) mounts this module as ``jettae agent``
and ``jettae.ocr.cli`` as ``jettae ocr``.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.table import Table

from jettae.agents.flows import AgentLimits, AgentReport, run_agent
from jettae.agents.planners import HeuristicPlanner, LLMPlanner, Planner
from jettae.agents.session import AgentSession, AuthFailed
from jettae.llm.base import LLMError
from jettae.llm.gateway import LLMMode, gateway_from_env

app = typer.Typer(help="Agent 실행(단일 vs 역할 분리, 같은 도구·예산)", no_args_is_help=True)
console = Console()


def _exit_code(report: AgentReport) -> int:
    return {"COMPLETED": 0, "LIMIT_REACHED": 0, "BLOCKED": 2}.get(report.status, 3)


@app.command("run")
def run_cmd(
    decision: Annotated[str, typer.Option("--decision", help="결정 id (예: dec:I1)")],
    strategy: Annotated[str, typer.Option("--strategy", help="single | roles")] = "single",
    mode: Annotated[str, typer.Option("--mode", help="replay | live | local")] = "replay",
    planner_name: Annotated[str, typer.Option("--planner", help="llm | heuristic")] = "llm",
    token: Annotated[
        str | None, typer.Option("--token", envvar="JETTAE_API_TOKEN", help="API 토큰 jtk_...")
    ] = None,
    as_of: Annotated[str | None, typer.Option("--as-of", help="기준일 YYYY-MM-DD")] = None,
    orchestrator: Annotated[str, typer.Option("--orchestrator")] = "auto",
    max_steps: Annotated[int, typer.Option("--max-steps")] = 16,
    max_tokens: Annotated[int, typer.Option("--max-llm-tokens")] = 200_000,
    max_seconds: Annotated[int, typer.Option("--max-seconds")] = 300,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """결정 하나를 Agent로 검토(엔진 수치는 코드가 계산, 승인·외부 발송 없음)."""
    if strategy not in ("single", "roles"):
        raise typer.BadParameter("--strategy must be single or roles")
    if mode not in ("replay", "live", "local"):
        raise typer.BadParameter("--mode must be replay, live or local")
    if orchestrator not in ("auto", "langgraph", "fallback"):
        raise typer.BadParameter("--orchestrator must be auto, langgraph or fallback")
    session = AgentSession.open()
    try:
        try:
            principal = session.principal(token)
        except AuthFailed as e:
            console.print(f"[red]인증 실패:[/] {e}")
            raise typer.Exit(code=2) from e
        planner: Planner
        if planner_name == "heuristic":
            planner = HeuristicPlanner()
        elif planner_name == "llm":
            try:
                planner = LLMPlanner(gateway_from_env(mode=LLMMode(mode)))
            except LLMError as e:
                console.print(f"[red]LLM 설정 오류:[/] {e}")
                raise typer.Exit(code=2) from e
        else:
            raise typer.BadParameter("--planner must be llm or heuristic")
        strat: Literal["single", "roles"] = "single" if strategy == "single" else "roles"
        orch: Literal["auto", "langgraph", "fallback"] = (
            "langgraph"
            if orchestrator == "langgraph"
            else "fallback"
            if orchestrator == "fallback"
            else "auto"
        )
        report = run_agent(
            session.runtime.service,
            principal.tenant_id,
            decision,
            strategy=strat,
            planner=planner,
            limits=AgentLimits(
                max_steps=max_steps,
                max_tool_calls=max_steps,
                max_llm_tokens=max_tokens,
                max_seconds=float(max_seconds),
            ),
            as_of=date.fromisoformat(as_of) if as_of else None,
            store=session.store,
            orchestrator=orch,
            actor=f"agent:{principal.user_id}",
        )
    finally:
        session.close()
    if as_json:
        typer.echo(json.dumps(report.to_json(), ensure_ascii=False, indent=1))
    else:
        _print(report)
    raise typer.Exit(code=_exit_code(report))


def _print(r: AgentReport) -> None:
    console.print(
        f"run {r.run_id}  strategy={r.strategy} orchestrator={r.orchestrator} "
        f"planner={r.planner} status={r.status}"
    )
    if r.error:
        console.print(f"[yellow]{r.error}[/]")
    t = Table("step", "role", "kind", "tool", "ok", "summary")
    for a in r.actions:
        t.add_row(
            str(a.step),
            a.role,
            a.kind,
            a.tool or "",
            "y" if a.ok else "n",
            (a.summary or a.note)[:60],
        )
    console.print(t)
    e = r.engine
    if e:
        console.print(f"엔진 결과: {e['status']}  필요 서류: {'; '.join(e['required_documents'])}")
        due = e.get("due") or {}
        for v in (due.get("outputs") or {}).get("variants", []):
            it = v["interest_total"]
            itxt = f"{it['amount']:,}원" if isinstance(it, dict) else "미계산"
            md = v["max_delay_days"]
            dtxt = f"최대 지연 {md}일" if md is not None else "지연일수 미계산(입금 배분 미확정)"
            console.print(f"  {v['label']}: 지급기한 {v['due_date']} {dtxt} 지연이자 {itxt}")
    for p in r.proposals:
        console.print(
            f"초안({p['type']}): {p.get('document') or p.get('would_change')} - 발송되지 않음"
        )
    console.print(f"usage: {json.dumps(r.usage, ensure_ascii=False)}  limits_hit={r.limits_hit}")
