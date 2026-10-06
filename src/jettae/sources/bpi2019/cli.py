"""``jettae sources bpi2019 fetch | convert | stats``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from jettae.sources import raw_dir, repo_root, sha256_file, utc_now_iso
from jettae.sources.bpi2019 import download
from jettae.sources.bpi2019.convert import iter_items_from_xes, write_items

app = typer.Typer(
    help="BPI Challenge 2019 (4TU.ResearchData, CC BY 4.0): 실제 구매·입고·송장·지급 기록",
    no_args_is_help=True,
)

EXIT_UNAVAILABLE = 3


def _items_path() -> Path:
    return raw_dir("bpi2019") / "po_items.jsonl.gz"


@app.command("fetch")
def fetch_cmd(
    file: Annotated[
        Path | None,
        typer.Option("--file", help="이미 받은 BPI_Challenge_2019.xes 경로(다운로드 생략)"),
    ] = None,
    retries: Annotated[int, typer.Option("--retries", min=1)] = 4,
    backoff: Annotated[float, typer.Option("--backoff", help="첫 재시도 대기(초), 2배씩")] = 30.0,
    md5: Annotated[
        str | None,
        typer.Option("--md5", help="--file 사용 시 4TU 목록에 게시된 md5(선택, 출처 확인용)"),
    ] = None,
) -> None:
    """4TU에서 XES 파일을 찾아 내려받고 manifest(data/manifests/bpi2019.json)에 기록."""
    if file is not None:
        try:
            p = download.register_local_file(file, expected_md5=md5)
        except download.ProvenanceMismatch as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(4) from None
        m = download.load_manifest()
        typer.echo(
            f"registered local file {p} (status {m.get('status')}: "
            f"{(m.get('file') or {}).get('provenance')}; data/manifests/bpi2019.json)"
        )
        return
    try:
        p = download.fetch(retries=retries, backoff_s=backoff, log=typer.echo)
    except download.SourceUnavailable as e:
        typer.echo(str(e), err=True)
        try:
            from jettae.evals.bpi_eval import record_pending

            record_pending("4TU.ResearchData unavailable at fetch time (maintenance).")
        except Exception as ee:  # pragma: no cover - reporting must not hide the real error
            typer.echo(f"(could not update docs/eval_results.md: {ee})", err=True)
        raise typer.Exit(EXIT_UNAVAILABLE) from None
    typer.echo(f"ok: {p}")


@app.command("convert")
def convert_cmd(
    file: Annotated[Path | None, typer.Option("--file", help="XES 경로(기본: manifest)")] = None,
    out: Annotated[Path | None, typer.Option("--out")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="앞의 N개 trace만")] = None,
) -> None:
    """XES -> 구매주문 품목별 레코드(JSONL.gz). 스트리밍 파싱."""
    src = file or download.local_log_path()
    if src is None:
        typer.echo(f"BPI 2019 log not available. Run `{download.FETCH_CMD}` first.", err=True)
        raise typer.Exit(EXIT_UNAVAILABLE)
    out = out or _items_path()
    n = write_items(iter_items_from_xes(src, limit=limit), out)
    m = download.load_manifest()
    reg = download.local_log_path()
    if reg is not None and src.resolve() == reg.resolve() and limit is None:
        try:
            rel = out.resolve().relative_to(repo_root().resolve()).as_posix()
        except ValueError:
            rel = str(out)
        m["converted"] = {
            "path": rel,
            "items": n,
            "sha256": sha256_file(out),
            "at": utc_now_iso(),
            "from_sha256": (m.get("file") or {}).get("sha256"),
        }
        download.save_manifest(m)
    typer.echo(f"wrote {n} PO items -> {out}")


@app.command("stats")
def stats_cmd(
    file: Annotated[
        Path | None, typer.Option("--file", help="XES 또는 변환된 JSONL(기본: 변환 결과)")
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
) -> None:
    """E4: 입고·송장·지급 연결률, 금액 불일치, 지급 소요일 분포(기술 통계)."""
    from jettae.evals import bpi_eval

    bpi_eval.run_cli(file, limit)
