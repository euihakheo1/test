"""E1/E2/E3 on 공정위 의결서 (real public data; SPEC §6) -- ``jettae eval ftc``.

This module runs the evaluation and writes its outputs; the work is split by responsibility:

- :mod:`.ftc_inputs`    -- seed files, cell parsing, variant labels
- :mod:`.ftc_rows`      -- one transcribed row -> engine recomputation and per-check results
- :mod:`.ftc_aggregate` -- E2 / E2-range / E2-cond / E3 summaries, E1 case-level checks
- :mod:`.ftc_report`    -- the markdown block in ``docs/eval_results.md``

Engine under test: :func:`jettae.rules.kr_retail.compute_due` (and ``compute_interest``).

- **E2** (rows with a base date): due date, delay days and interest recomputed with every
  variant ``rollover ∈ {off, on} x rounding ∈ {floor, half_up}``; exact match of due date /
  delay days, interest within ±1 KRW (±1,000 KRW when the table unit is 천원). Range rows
  ("기간 a ~ b") only check the due dates of both endpoints.
- **E2-cond** (no base date, table due date present): delay days and interest recomputed
  from the table's own due date -- arithmetic only, not the statutory due date.
- **E3** (abstention): rows without a base date must yield ``Insufficient`` with required
  documents; rows with a base date must not abstain.
- **E1** (case level): text totals vs printed totals; engine sums only for tables whose rows
  are all transcribed (no "⋮").

Metric definitions (easy to misread; see also docs/eval_protocol.md):

- a check is *evaluable* only when both the engine value and the table value exist;
  a non-evaluable check is ``None`` and is never counted as a match;
- ``all_three_ok``: due date, delay days AND interest were all evaluated and all match.
  Denominator: every E2 row (a row whose table lacks a value cannot pass);
- ``all_available_ok``: every *evaluable* check matches (at least one evaluable). Weaker;
  reported under that name only;
- ``post_hoc_any_variant`` ("제시한 계산 중 정답을 포함한 비율"): a row counts when ANY of the
  four variants matches. The variant is chosen after seeing the answer, so this is an upper
  bound, not the accuracy of a configuration the product would pick in advance;
- ``abstained`` / ``engine_full_computation`` / ``evaluable_checks``: how often the engine
  declined, computed all three values, and how many checks each row could be scored on.

Wording: the report describes matches/differences only; it makes no legal judgement.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jettae.evals.ftc_aggregate import (
    evaluate_e1,
    summarize_cond,
    summarize_e2,
    summarize_e3,
    summarize_range,
)
from jettae.evals.ftc_inputs import (
    ROW_COLUMNS,
    TABLE_COLUMNS,
    VARIANTS,
    SeedRow,
    TableMeta,
    load_facts,
    load_rows,
    load_tables,
    stated_rates,
    variant_label,
)
from jettae.evals.ftc_report import MD_END, MD_START, render_markdown, update_markdown
from jettae.evals.ftc_rows import RowResult, VariantCheck, _interest_ok, evaluate_row
from jettae.rules.kr_retail import builtin_registry
from jettae.sources.ftc.paths import long_path, results_dir, seeds_dir

__all__ = [
    "MD_END",
    "MD_START",
    "ROW_COLUMNS",
    "TABLE_COLUMNS",
    "VARIANTS",
    "RowResult",
    "SeedRow",
    "TableMeta",
    "VariantCheck",
    "_interest_ok",
    "code_hash",
    "evaluate_row",
    "load_rows",
    "load_tables",
    "display_path",
    "render_markdown",
    "run",
    "update_markdown",
    "write_outputs",
]

REPO_DOCS = Path(__file__).resolve().parents[3] / "docs"


# ------------------------------------------------------------------ run
def _sha256_file(p: Path) -> str | None:
    lp = long_path(p)
    return hashlib.sha256(lp.read_bytes()).hexdigest() if lp.exists() else None


def code_hash() -> str:
    h = hashlib.sha256()
    pkg = Path(__file__).resolve().parents[1]
    for rel in (
        "evals/ftc_eval.py",
        "evals/ftc_inputs.py",
        "evals/ftc_rows.py",
        "evals/ftc_aggregate.py",
        "evals/ftc_report.py",
        "rules/kr_retail.py",
        "rules/calendar_kr.py",
        "domain/money.py",
        "domain/dates.py",
    ):
        p = pkg / rel
        h.update(rel.encode())
        h.update(long_path(p).read_bytes())
    return h.hexdigest()


def run(
    rows_path: Path | None = None,
    tables_path: Path | None = None,
    facts_path: Path | None = None,
) -> dict[str, Any]:
    rows_path = rows_path or seeds_dir() / "ftc_rows.csv"
    tables_path = tables_path or seeds_dir() / "ftc_tables.csv"
    facts_path = facts_path or seeds_dir() / "ftc_case_facts.jsonl"
    rows = load_rows(rows_path)
    tables = load_tables(tables_path)
    facts = load_facts(facts_path)
    rates = stated_rates(facts)
    results = [evaluate_row(r, tables.get(r.table_key), rates.get(r["decision_id"])) for r in rows]
    reg = builtin_registry()
    report = {
        "eval": "ftc",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "inputs": {
            "rows": {
                "path": "data/seeds/ftc_rows.csv",
                "sha256": _sha256_file(rows_path),
                "n": len(rows),
            },
            "tables": {
                "path": "data/seeds/ftc_tables.csv",
                "sha256": _sha256_file(tables_path),
                "n": len(tables),
            },
            "facts": {
                "path": "data/seeds/ftc_case_facts.jsonl",
                "sha256": _sha256_file(facts_path),
                "n": len(facts),
            },
            "decisions": sorted({r["decision_id"] for r in rows}, key=int),
            "images": len({r.table_key for r in rows}),
            "transcription": "claude-agent, verified=no (see data/seeds/ftc_rows.README.md)",
        },
        "engine": {
            "code_sha256": code_hash(),
            "rule_versions": [rv.key for rv in reg if rv.effective_from is not None],
            "variants": [variant_label(r, m) for r, m in VARIANTS],
            "tolerance": "interest ±1 KRW; ±1,000 KRW when the table unit is 천원",
        },
        "stated_rates": rates,
        "E2": {
            "all_rows": summarize_e2(results),
            "certain_rows_only": summarize_e2(results, certain_only=True),
            "range_rows": summarize_range(results),
            "no_base_date_rows_conditional_on_table_due": summarize_cond(results),
        },
        "E3": summarize_e3(results, rows),
        "E1": evaluate_e1(tables, rows, results, facts),
        "rows": [r.to_json() for r in results],
    }
    return report


def write_outputs(
    report: dict[str, Any],
    *,
    docs: Path | None = None,
    out: Path | None = None,
    md: Path | None = None,
) -> tuple[Path, Path]:
    """Write the JSON results and the markdown block. ``out``/``md`` redirect both outputs
    (e.g. for a reviewer re-run that must not touch the tracked files)."""
    out = out or results_dir() / "ftc_eval.json"
    long_path(out.parent).mkdir(parents=True, exist_ok=True)
    long_path(out).write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    md_path = md or (docs or REPO_DOCS) / "eval_results.md"
    update_markdown(md_path, render_markdown(report, results_path=display_path(out)))
    return out, md_path


def display_path(path: Path) -> str:
    """``path`` relative to the repository when it lies inside it, else absolute (POSIX
    separators), for citing the results file in the markdown."""
    p = Path(path).resolve()
    try:
        return p.relative_to(REPO_DOCS.parent.resolve()).as_posix()
    except ValueError:
        return p.as_posix()
