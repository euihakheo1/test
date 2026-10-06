"""Second external review (2026-10-06): E1 sums that use the rate stated in the decision are
labelled as such (finding 9), and the markdown cites the JSON file it was written with
(finding 11). Hand-written fixtures only (the ``report`` fixture of test_ev_ftc_metrics)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from test_ev_ftc_metrics import report  # noqa: F401  (pytest fixture)

from jettae.evals import ftc_eval as fe
from jettae.evals.ftc_aggregate import _engine_total
from jettae.evals.ftc_inputs import VARIANTS, TableMeta, variant_label
from jettae.evals.ftc_rows import RowResult, VariantCheck


def _row(key: str, *, engine: int | None, stated: int | None) -> RowResult:
    r = RowResult(key, "16947", "1", "표 5", "e2", False)
    for rollover, rounding in VARIANTS:
        lab = variant_label(rollover, rounding)
        r.variants.append(VariantCheck(lab, None, 10, engine, None, None, None))
        if stated is not None:
            r.arith[lab] = {
                "computed": True,
                "rate_percent": "15.5",
                "floor": {"interest_krw": stated},
                "half_up": {"interest_krw": stated},
            }
    r.interest_engine_missing = engine is None
    return r


META = TableMeta({"decision_id": "16947", "table_label": "표 5", "rows_elided": "no"})


def _total(rows: list[RowResult]) -> dict[str, Any]:
    return _engine_total("total_interest", [META], {("16947", "표 5"): rows}, {})


def test_engine_total_says_where_the_rate_came_from():
    stated = _total([_row("a", engine=None, stated=40), _row("b", engine=None, stated=2)])
    assert stated["interest_source"] == "stated_rate"
    assert (stated["rows_registry_rate"], stated["rows_stated_rate"]) == (0, 2)
    assert stated["engine_sum_by_variant_krw"]["rollover_off/floor"] == 42
    engine = _total([_row("a", engine=5, stated=None)])
    assert engine["interest_source"] == "registry" and engine["rows_stated_rate"] == 0
    mixed = _total([_row("a", engine=5, stated=None), _row("b", engine=None, stated=2)])
    assert mixed["interest_source"] == "mixed"


def test_markdown_separates_stated_rate_sums_and_cites_its_json(
    tmp_path: Path,
    report: dict[str, Any],  # noqa: F811
) -> None:
    rep = copy.deepcopy(report)
    summary = rep["E1"]["summary"]
    assert set(summary["engine_computable_by_source"]) == {
        "registry",
        "stated_rate",
        "mixed",
        "no_rate",
    }
    check = next(c for c in rep["E1"]["checks"] if "engine_sum_by_variant_krw" in c)
    check.update(interest_source="stated_rate", rows_registry_rate=0, rows_stated_rate=7)
    summary["engine_computable_by_source"] = {
        "registry": 0,
        "stated_rate": summary["engine_computable"],
        "mixed": 0,
        "no_rate": 0,
    }
    md = fe.render_markdown(rep, results_path="/tmp/f.json")
    assert "(results: `/tmp/f.json`)" in md and "data/results/ftc_eval.json" not in md
    assert "engine / 연번" not in md and "computed / 연번 (source)" in md
    assert "engine delay days + rate stated in the decision; rows: registry rate 0, " in md
    assert "stated rate 7" in md
    assert "partly circular" in md

    # write_outputs cites the file it actually wrote
    out, md_path = fe.write_outputs(report, out=tmp_path / "r.json", md=tmp_path / "r.md")
    text = md_path.read_text(encoding="utf-8")
    assert f"(results: `{(tmp_path / 'r.json').resolve().as_posix()}`)" in text
    assert fe.display_path(fe.REPO_DOCS.parent / "data" / "results" / "ftc_eval.json") == (
        "data/results/ftc_eval.json"
    )
