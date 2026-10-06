"""``jettae sources law fetch | check | contract-fetch | contract-extract``."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from jettae.sources import results_dir, utc_now_iso, write_json
from jettae.sources.law import check as law_check
from jettae.sources.law import drf

app = typer.Typer(
    help="법령·고시 원문(법제처 DRF)과 공정위 표준거래계약서: 수집·규칙 대조·조항 추출",
    no_args_is_help=True,
)
console = Console()


@app.command("fetch")
def fetch_cmd() -> None:
    """대규모유통업법 제8조·하도급법 제13조·지연이율 고시 원문을 받아 manifest에 기록.

    OC 키: 환경변수 JETTAE_DRF_OC (기본 'test')."""
    m = drf.fetch_all(log=typer.echo)
    required = {s.key for s in drf.SPECS if s.required}
    missing = [k for k in m["failures"] if k in required]
    typer.echo(f"manifest: data/manifests/law.json (failures: {m['failures'] or 'none'})")
    if missing:
        raise typer.Exit(3)


@app.command("check")
def check_cmd(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """rules.kr_retail의 기한(60/40일)·이율(15.5%)·시행일을 원문과 대조하고 근거 문장 출력."""
    m = drf.load_manifest()
    if m is None:
        typer.echo(
            "data/manifests/law.json missing: run `uv run jettae sources law fetch`", err=True
        )
        raise typer.Exit(3)
    items = law_check.run_checks(m)
    bad = law_check.failed(items)
    out = {
        "checked_at": utc_now_iso(),
        "manifest_fetched_at": m.get("fetched_at"),
        "ok": not bad,
        "items": [c.to_json() for c in items],
    }
    write_json(results_dir() / "law_check.json", out)
    if as_json:
        typer.echo(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        t = Table("rule", "param", "registry", "source", "status")
        for c in items:
            t.add_row(c.rule, c.param, c.registry_value, str(c.found_value), c.status)
        console.print(t)
        for c in items:
            if c.evidence:
                ev = c.evidence
                if c.span:
                    a, b = c.span
                    ev = ev[:a] + "[" + ev[a:b] + "]" + ev[b:]
                console.print(f"- {c.rule} {c.param} ({c.source_key}): {ev}", highlight=False)
                if c.note:
                    console.print(f"    note: {c.note}", highlight=False)
        console.print(
            f"source fetched at {m.get('fetched_at')}; result -> data/results/law_check.json"
        )
    if bad:
        raise typer.Exit(1)


@app.command("contract-fetch")
def contract_fetch_cmd() -> None:
    """공정위 표준유통거래계약서(직매입·특약매입·위수탁) 첨부 파일을 받아 manifest에 기록."""
    from jettae.sources.contract import ftc_board

    m = ftc_board.fetch_contracts(log=typer.echo)
    ok = sum(1 for d in m["documents"] if d["ok"])
    typer.echo(
        f"{ok}/{len(m['documents'])} downloaded -> data/manifests/contract.json"
        + (f" (board error: {m['board_error']})" if m.get("board_error") else "")
    )
    if ok == 0:
        raise typer.Exit(3)


@app.command("contract-extract")
def contract_extract_cmd() -> None:
    """E5: 받은 계약서에서 지급기한·기산점 조항을 원문 위치와 함께 추출."""
    from jettae.evals import contract_eval

    try:
        res = contract_eval.run()
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(3) from None
    for r in res["documents"]:
        if r["status"] != "extracted":
            typer.echo(f"- {r.get('title')}: {r['status']} {r.get('error') or ''}")
            continue
        typer.echo(f"- {r['title']} [{r['parsed_as']}, {r['paragraphs']} paragraphs]")
        for c in r["clauses"]:
            loc = c["locator"]
            typer.echo(
                f"    {c['kind']:<13} 제{c['article_no']}조 s{loc['section']}p{loc['paragraph']}"
                f" chars {loc['char_start']}-{loc['char_end']}: {c['text'].strip()[:160]}"
            )
    s = res["summary"]
    typer.echo(f"summary: {json.dumps(s, ensure_ascii=False)} -> data/results/contract_eval.json")
