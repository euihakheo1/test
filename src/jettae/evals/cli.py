"""``jettae eval ...`` — evaluations on real public data (SPEC §6).

``ftc`` is defined here. Other evaluation modules are mounted lazily when they exist:
``jettae.evals.bpi_eval`` (E4), ``jettae.evals.contract_eval`` (E5),
``jettae.evals.recompute_eval`` (E6). Each may expose ``app: typer.Typer`` (mounted as a
sub-group) or a ``main``/``run_cli`` callable (mounted as a single command).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

app = typer.Typer(help="실제 공개 자료 평가(E1~E6)", no_args_is_help=True)
console = Console()

# (module, command name)
OPTIONAL_EVALS: tuple[tuple[str, str], ...] = (
    ("jettae.evals.bpi_eval", "bpi"),
    ("jettae.evals.contract_eval", "contract"),
    ("jettae.evals.recompute_eval", "recompute"),
)
EVAL_STATUS: dict[str, str] = {}


@app.command("ftc")
def ftc_cmd(
    print_md: bool = typer.Option(False, "--print", help="마크다운 요약을 화면에도 출력"),
    out: Path | None = typer.Option(None, "--out", help="결과 JSON 경로(기본 data/results)"),
    md: Path | None = typer.Option(None, "--md", help="마크다운 경로(기본 docs/eval_results.md)"),
) -> None:
    """E1/E2/E3: 공정위 의결서 전사 행 재계산 → data/results/ftc_eval.json, docs/eval_results.md."""
    from jettae.evals.ftc_eval import display_path, render_markdown, run, write_outputs

    try:
        report = run()
    except FileNotFoundError as e:
        console.print(f"[red]입력 파일 없음:[/] {e}")
        raise typer.Exit(code=1) from e
    out, md = write_outputs(report, out=out, md=md)
    e2 = report["E2"]["all_rows"]
    e3 = report["E3"]
    strict = ", ".join(
        f"{lab}={per['all_three_ok']['n']}/{per['all_three_ok']['of']}"
        for lab, per in e2["per_variant"].items()
    )
    post = e2["post_hoc_any_variant"]["all_three_ok"]
    console.print(
        f"E2 rows={e2['rows']} all-three-evaluated-and-match: {strict}; "
        f"post-hoc any variant={post['n']}/{post['of']}; "
        f"E3 abstained={e3['abstained_correctly']['n']}/{e3['abstained_correctly']['of']}; "
        f"E1 checks={report['E1']['summary']['facts_linked_to_transcribed_tables']}"
    )
    console.print(f"-> {out}\n-> {md}")
    if print_md:
        console.print(
            render_markdown(report, results_path=display_path(out)), markup=False, highlight=False
        )


def _mount(module: str, name: str) -> None:
    try:
        mod: Any = importlib.import_module(module)
    except ModuleNotFoundError as e:
        if e.name and (module == e.name or module.startswith(e.name + ".")):
            EVAL_STATUS[module] = "not installed (skipped)"
            return
        EVAL_STATUS[module] = f"import error: {e!r}"
        return
    except ImportError as e:  # pragma: no cover - defensive
        EVAL_STATUS[module] = f"import error: {e!r}"
        return
    sub = getattr(mod, "app", None)
    if isinstance(sub, typer.Typer):
        app.add_typer(sub, name=name)
        EVAL_STATUS[module] = "loaded (sub-app)"
        return
    fn = getattr(mod, "main", None) or getattr(mod, "run_cli", None)
    if callable(fn):
        app.command(name)(fn)
        EVAL_STATUS[module] = "loaded (command)"
        return
    EVAL_STATUS[module] = "loaded, but no `app`/`main`/`run_cli` (skipped)"


for _module, _name in OPTIONAL_EVALS:
    _mount(_module, _name)
