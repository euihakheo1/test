"""``jettae sources ftc ...`` — collect real 공정위 의결서 data from the 법제처 DRF API."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from jettae.sources.ftc.client import DEFAULT_OC, DrfClient, DrfError, drf_oc
from jettae.sources.ftc.collect import (
    DEFAULT_QUERIES,
    QuerySpec,
    download_images,
    fetch,
    load_manifest,
    write_facts,
)
from jettae.sources.ftc.extract import extract_case_facts
from jettae.sources.ftc.parse import delay_table_score, parse_decision

app = typer.Typer(
    help="공정위 의결서(법제처 DRF target=ftc): 수집·표 이미지·사건 단위 사실 추출",
    no_args_is_help=True,
)
console = Console()


def _client(min_interval: float, offline: bool = False) -> DrfClient:
    if drf_oc() == DEFAULT_OC and not offline:
        console.print(
            "[dim]OC=test (DRF 샘플 키, 개발용). 운영에서는 open.law.go.kr에서 발급한 "
            "본인 OC를 JETTAE_DRF_OC로 지정하세요.[/]"
        )
    return DrfClient(min_interval=min_interval, offline=offline)


@app.command("fetch")
def fetch_cmd(
    limit: Annotated[int | None, typer.Option("--limit", help="가져올 의결서 수 상한")] = None,
    query: Annotated[
        list[str] | None,
        typer.Option("--query", help="추가 검색어(사건명 검색). 지정 시 기본 검색어 대신 사용"),
    ] = None,
    body: Annotated[bool, typer.Option("--body", help="--query를 본문 검색으로")] = False,
    refresh: Annotated[bool, typer.Option("--refresh", help="캐시 무시하고 다시 받기")] = False,
    offline: Annotated[bool, typer.Option("--offline", help="캐시만 사용(네트워크 금지)")] = False,
    interval: Annotated[float, typer.Option("--interval", help="요청 간 최소 간격(초)")] = 1.0,
) -> None:
    """검색 → 의결서 XML 수집(캐시) → 파싱 → data/manifests/ftc.json, seeds/ftc_case_facts.jsonl."""
    queries = (
        tuple(QuerySpec(f"custom{i}", q, body=body) for i, q in enumerate(query))
        if query
        else DEFAULT_QUERIES
    )
    with _client(interval, offline) as client:
        try:
            rep = fetch(
                client,
                queries=queries,
                limit=limit,
                refresh=refresh,
                progress=lambda s: console.print(s, highlight=False),
            )
        except DrfError as e:
            console.print(f"[red]DRF 접근 실패:[/] {e}")
            raise typer.Exit(code=1) from e
    for q in rep.queries:
        console.print(
            f"query '{q['query']}' ({q['search']}): total={q['total']} selected={q['selected']}"
        )
    console.print(
        f"decisions: ok={len(rep.ok)} failed={len(rep.failures)} "
        f"network_requests={rep.network_requests}"
    )
    if rep.failures:
        raise typer.Exit(code=3)


@app.command("images")
def images_cmd(
    limit: Annotated[int | None, typer.Option("--limit", help="받을 이미지 수 상한")] = None,
    decision: Annotated[
        list[str] | None, typer.Option("--decision", help="결정문일련번호(여러 번 지정 가능)")
    ] = None,
    min_score: Annotated[int, typer.Option("--min-score", help="1=느슨, 2=표 제목 기준")] = 2,
    interval: Annotated[float, typer.Option("--interval")] = 1.0,
    offline: Annotated[bool, typer.Option("--offline")] = False,
) -> None:
    """지연·지급·이자 표로 보이는 표 이미지(flSeq)를 data/raw/ftc/img에 내려받기."""
    with _client(interval, offline) as client:
        try:
            out = download_images(
                client,
                min_score=min_score,
                decision_ids=decision,
                limit=limit,
                progress=lambda s: console.print(s, highlight=False),
            )
        except FileNotFoundError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(code=1) from e
    c = out["counts"]
    console.print(
        f"images ok={c['ok']} failed={c['failed']} unlinked(no flSeq)={c['unlinked']} "
        f"decisions={c['decisions']} -> data/manifests/ftc_images.json"
    )


@app.command("facts")
def facts_cmd() -> None:
    """캐시된 XML에서 사건 단위 사실을 다시 추출 → data/seeds/ftc_case_facts.jsonl."""
    man = load_manifest()
    rows = []
    with DrfClient(offline=True) as client:
        for d in man["decisions"]:
            if d.get("status") != "ok":
                continue
            got = client.fetch_decision(d["decision_id"])
            for f in extract_case_facts(parse_decision(got.read_bytes())):
                rows.append({**f.to_json(), "doc_sha256": got.sha256})
    path = write_facts(rows)
    console.print(f"{len(rows)} facts -> {path}")


@app.command("show")
def show_cmd(
    decision_id: str,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    offline: Annotated[bool, typer.Option("--offline")] = False,
) -> None:
    """의결서 하나의 표 이미지 목록과 추출 사실 표시."""
    with _client(1.0, offline) as client:
        dec = parse_decision(client.fetch_decision(decision_id).read_bytes())
    facts = extract_case_facts(dec)
    if as_json:
        typer.echo(json.dumps([f.to_json() for f in facts], ensure_ascii=False, indent=1))
        return
    console.print(f"{dec.decision_id} {dec.case_no} {dec.decision_date} {dec.title}")
    t = Table("#", "section", "label", "flSeq", "score", "caption", "unit")
    for ti in dec.tables:
        s, _ = delay_table_score(ti)
        t.add_row(
            str(ti.index),
            ti.section,
            ti.label or "",
            ti.flseq or "(unlinked)",
            str(s),
            ti.caption or "",
            ti.unit_note or "",
        )
    console.print(t)
    f = Table("kind", "value", "conf", "masked", "fn", "where", "tables")
    for x in facts:
        v = x.value if not isinstance(x.value, str) else x.value[:60]
        f.add_row(
            x.kind,
            str(v),
            x.confidence,
            "y" if x.masked else "",
            "y" if x.in_footnote else "",
            f"{x.section}:{x.char_start}-{x.char_end}",
            ",".join(x.table_refs),
        )
    console.print(f)
