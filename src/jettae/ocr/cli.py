"""``jettae ocr ...`` -- VLM transcription of downloaded 공정위 table images, comparison.

``ftc-tables`` reads ``data/manifests/ftc_images.json`` (written by
``jettae sources ftc images``), checks each image's sha256 against the manifest and sends it
to the VLM through the gateway. Default mode comes from ``JETTAE_LLM_MODE`` (offline): then
only recorded responses are replayed and nothing is sent. Output is a cells CSV under
``data/raw/ftc/ocr/`` (gitignored) marked ``verified=no``; it never overwrites the FTC
task's ``data/seeds/ftc_rows.csv``.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from jettae.llm.base import LiveCallRefused, LLMError, ReplayMiss
from jettae.llm.gateway import LLMMode, gateway_from_env
from jettae.ocr.base import TableImage, TranscribedTable
from jettae.ocr.manual import compare_tables, load_cells_csv, write_cells_csv
from jettae.ocr.vlm import VlmTableTranscriber

app = typer.Typer(help="표 이미지 전사(VLM, live 전용·예산 제한)와 전사 비교", no_args_is_help=True)
console = Console()


def _data_dir() -> Path:
    from jettae.sources.ftc.paths import data_dir

    return data_dir()


def select_images(
    manifest: dict[str, Any],
    *,
    flseqs: list[str] | None,
    decisions: list[str] | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    out = []
    for im in manifest.get("images", []):
        if im.get("status") != "ok" or not im.get("flseq"):
            continue
        if flseqs and im["flseq"] not in flseqs:
            continue
        if decisions and im.get("decision_id") not in decisions:
            continue
        out.append(im)
    return out[:limit] if limit else out


def load_image(entry: dict[str, Any], data_dir: Path) -> TableImage:
    path = data_dir / entry["path"]
    data = path.read_bytes()
    h = hashlib.sha256(data).hexdigest()
    if entry.get("sha256") and h != entry["sha256"]:
        raise ValueError(f"{path}: sha256 {h[:12]} does not match manifest {entry['sha256'][:12]}")
    ctx = {
        k: entry.get(k)
        for k in ("decision_id", "case_no", "table_label", "caption", "unit_note", "flseq")
    }
    return TableImage(data, str(entry["flseq"]), "image/png", ctx)


@app.command("ftc-tables")
def ftc_tables(
    flseq: Annotated[list[str] | None, typer.Option("--flseq", help="flSeq (반복 가능)")] = None,
    decision: Annotated[list[str] | None, typer.Option("--decision", help="결정문일련번호")] = None,
    limit: Annotated[int, typer.Option("--limit", help="처리할 이미지 수 상한")] = 5,
    mode: Annotated[
        str | None, typer.Option("--mode", help="replay | live (기본: 환경변수)")
    ] = None,
    out: Annotated[Path | None, typer.Option("--out", help="cells CSV 경로")] = None,
) -> None:
    """공정위 표 이미지 → VLM 전사(cells CSV, verified=no)."""
    dd = _data_dir()
    man_path = dd / "manifests" / "ftc_images.json"
    if not man_path.exists():
        console.print(f"[red]{man_path} 없음[/] - 먼저 `jettae sources ftc images` 실행")
        raise typer.Exit(code=1)
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    images = select_images(manifest, flseqs=flseq, decisions=decision, limit=limit)
    if not images:
        console.print("선택된 이미지 없음")
        raise typer.Exit(code=1)
    gw = gateway_from_env(mode=LLMMode(mode) if mode else None)
    tr = VlmTableTranscriber(gw)
    done: list[TranscribedTable] = []
    blocked = failed = 0
    for entry in images:
        try:
            img = load_image(entry, dd)
            t = tr.transcribe(img)
        except (ReplayMiss, LiveCallRefused) as e:
            blocked += 1
            console.print(f"[yellow]{entry['flseq']}: 전송하지 않음[/] - {e}")
            continue
        except (LLMError, OSError, ValueError) as e:
            failed += 1
            console.print(f"[red]{entry['flseq']}: 실패[/] - {e}")
            continue
        done.append(t)
        console.print(
            f"{t.source_id}: rows={len(t.rows)} illegible={len(t.illegible)} "
            f"model={t.model} cached={t.meta.get('from_cache')} cost_krw={t.meta.get('cost_krw')}"
        )
    path = out or (dd / "raw" / "ftc" / "ocr" / "vlm_cells.csv")
    if done:
        n = write_cells_csv(path, done)
        console.print(f"{n} cells -> {path} (verified=no)")
    tot = gw.totals()
    console.print(
        f"images ok={len(done)} blocked={blocked} failed={failed}; llm calls={tot['calls']} "
        f"cached={tot['cached_calls']} cost_krw={Decimal(tot['cost_krw'])}"
    )
    if blocked or failed:
        raise typer.Exit(code=2 if not failed else 3)


@app.command("compare")
def compare_cmd(
    a: Annotated[Path, typer.Argument(help="cells CSV A")],
    b: Annotated[Path, typer.Argument(help="cells CSV B")],
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """같은 이미지에 대한 두 전사(cells CSV)의 셀 일치."""
    ta, tb = load_cells_csv(a), load_cells_csv(b)
    results = [compare_tables(ta[s], tb[s]) for s in sorted(set(ta) & set(tb))]
    only = {"only_a": sorted(set(ta) - set(tb)), "only_b": sorted(set(tb) - set(ta))}
    if as_json:
        typer.echo(json.dumps({"tables": results, **only}, ensure_ascii=False, indent=1))
        return
    for r in results:
        console.print(
            f"{r['source_id']}: {r['agreement']} cells agree (rows {r['rows_a']}/{r['rows_b']})"
        )
    console.print(json.dumps(only, ensure_ascii=False))
