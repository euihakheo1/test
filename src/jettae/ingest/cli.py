"""``jettae ingest inspect <file>`` and ``jettae ingest parse <file> --out rows.json``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table as RTable

from jettae.domain.hashing import to_canonical
from jettae.domain.status import DocumentStatus
from jettae.ingest.detect import safe_parse
from jettae.ingest.formats import FORMATS_BY_ID
from jettae.ingest.formats.base import IngestOptions
from jettae.ingest.pipeline import analyze, build_records, plan_summary
from jettae.ingest.security import DEFAULT_LIMITS, safe_filename

app = typer.Typer(help="업로드 파일 읽기: 양식 인식·열 매핑·원문 위치", no_args_is_help=True)
console = Console()

EXIT = {
    DocumentStatus.PARSED: 0,
    DocumentStatus.NEEDS_MAPPING: 2,
    DocumentStatus.UNSUPPORTED_SCAN: 3,
    DocumentStatus.CORRUPT: 4,
    DocumentStatus.FAILED: 4,
}


def _read(path: Path) -> bytes:
    if not path.is_file():
        raise typer.BadParameter(f"not a file: {path}")
    size = path.stat().st_size
    if size > DEFAULT_LIMITS.max_bytes:
        raise typer.BadParameter(f"file is {size} bytes; limit is {DEFAULT_LIMITS.max_bytes}")
    return path.read_bytes()


def _parse_map(items: list[str] | None) -> dict[str, dict[str, int | str | None]]:
    if not items:
        return {}
    out: dict[str, int | str | None] = {}
    for it in items:
        if "=" not in it:
            raise typer.BadParameter(f"--map expects field=column, got {it!r}")
        k, v = it.split("=", 1)
        v = v.strip()
        out[k.strip()] = None if v == "" else (int(v) if v.isdigit() else v)
    return {"*": out}


def _options(
    fmt: str | None,
    counterparty: str | None,
    self_brn: str | None,
    direction: str | None,
    accept: bool,
    maps: list[str] | None,
) -> IngestOptions:
    if fmt and fmt not in FORMATS_BY_ID:
        raise typer.BadParameter(f"unknown format {fmt!r}; known: {', '.join(FORMATS_BY_ID)}")
    if direction and direction not in ("sales", "purchase"):
        raise typer.BadParameter("--direction must be sales or purchase")
    return IngestOptions(
        counterparty_override=counterparty,
        self_brn=self_brn,
        direction=direction,
        accept_suggested=accept,
        format_id=fmt,
        mapping=_parse_map(maps),
    )


FormatOpt = Annotated[str | None, typer.Option("--format", help="양식 강제 지정")]
CpOpt = Annotated[
    str | None, typer.Option("--counterparty", help="모든 행에 적용할 거래처 이름(열보다 우선)")
]
BrnOpt = Annotated[str | None, typer.Option("--self-brn", help="우리 회사 사업자등록번호")]
DirOpt = Annotated[str | None, typer.Option("--direction", help="sales | purchase")]
AcceptOpt = Annotated[
    bool, typer.Option("--accept-suggested", help="확인되지 않은 열 매핑 제안을 그대로 사용")
]
MapOpt = Annotated[
    list[str] | None, typer.Option("--map", help="열 매핑 확정: field=열번호(0부터) 또는 열 이름")
]


@app.command("inspect")
def inspect_cmd(
    file: Annotated[Path, typer.Argument(help="CSV / XLSX / PDF 파일")],
    fmt: FormatOpt = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    maps: MapOpt = None,
) -> None:
    """양식 판별과 열 매핑 제안을 보여 준다(레코드는 만들지 않음)."""
    content = _read(file)
    parsed = safe_parse(content, safe_filename(file.name))
    plan = analyze(parsed, _options(fmt, None, None, None, False, maps))
    summary = plan_summary(plan)
    if as_json:
        typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
        raise typer.Exit(EXIT.get(plan.status, 4))
    console.print(
        f"[bold]{summary['file']}[/] kind={summary['source_kind']} status={summary['status']} "
        f"encoding={summary['encoding']} delimiter={summary['delimiter']!r}"
    )
    if summary["reason"]:
        console.print(f"사유: {summary['reason']}")
    for w in summary["warnings"] + summary["notes"]:
        console.print(f"  참고: {w}")
    for t in summary["tables"]:
        console.print(
            f"\n[bold]{t['table']}[/] header rows {t['header_rows']} data {t['data_rows']} "
            f"합계행 {t['total_rows']} → format={t['format']} usable={t['usable']} {t['reason']}"
        )
        m = t["mapping"]
        if not m:
            continue
        rt = RTable("column", "header", "field", "confidence", "reason")
        for x in m["matches"]:
            rt.add_row(str(x["column"]), x["header"], x["field"], x["confidence"], x["reason"])
        console.print(rt)
        if m["unmapped_required"]:
            console.print("  매핑되지 않은 필수 항목: " + ", ".join(m["unmapped_required"]))
    raise typer.Exit(EXIT.get(plan.status, 4))


@app.command("parse")
def parse_cmd(
    file: Annotated[Path, typer.Argument(help="CSV / XLSX / PDF 파일")],
    out: Annotated[Path, typer.Option("--out", help="결과 JSON 경로")],
    fmt: FormatOpt = None,
    counterparty: CpOpt = None,
    self_brn: BrnOpt = None,
    direction: DirOpt = None,
    accept: AcceptOpt = False,
    maps: MapOpt = None,
    tenant: Annotated[str, typer.Option("--tenant")] = "local",
) -> None:
    """행·셀(원문 위치 포함)과 인식된 레코드·사실을 JSON으로 저장한다."""
    content = _read(file)
    opts = _options(fmt, counterparty, self_brn, direction, accept, maps)
    parsed = safe_parse(content, safe_filename(file.name))
    plan = analyze(parsed, opts)
    res = build_records(
        plan, tenant_id=tenant, doc_version_id=f"file:{safe_filename(file.name)}", options=opts
    )
    payload = {
        "summary": plan_summary(plan),
        "tables": [
            {
                "name": t.name,
                "rows": [
                    {
                        "row": r.index,
                        "cells": [{"text": c.text, "locator": dict(c.locator)} for c in r.cells],
                    }
                    for r in t.rows
                ],
            }
            for t in parsed.tables
        ],
        "counts": res.counts.model_dump(mode="json"),
        "records": to_canonical(res.records),
        "facts": to_canonical(res.facts),
        "issues": [
            {
                "table": i.table,
                "row": i.row,
                "field": i.field,
                "message": i.message,
                "kind": i.kind,
            }
            for i in res.issues
        ],
        "totals": [
            {
                "table": t.table,
                "row": t.row,
                "field": t.field,
                "stated": t.stated,
                "computed": t.computed,
                "matches": t.matches,
            }
            for t in res.totals
        ],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    console.print(
        f"status={plan.status.value} records={len(res.records)} facts={len(res.facts)} "
        f"issues={len(res.issues)} → {out}"
    )
    raise typer.Exit(EXIT.get(plan.status, 4))
