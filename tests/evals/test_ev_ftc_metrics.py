"""FTC evaluation (E1/E2/E3): scoring and metric definitions on small hand-written fixtures.

These rows are unit-test fixtures (tests/ only); they are never part of evaluation results.
Replaced the former tests/sources_ftc/test_ftc_eval.py (same fixtures; assertions updated for the
separated "all three" / "all available" / post-hoc metrics).
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from jettae.evals import ftc_eval as fe
from jettae.evals.ftc_aggregate import summarize_e2
from jettae.evals.ftc_inputs import variant_label
from jettae.evals.ftc_rows import RowResult, VariantCheck

TABLE_DEFAULTS = dict.fromkeys(fe.TABLE_COLUMNS, "")


def _write_csv(path: Path, cols: tuple[str, ...], rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(cols))
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    return path


def _row(**kw: str) -> dict[str, str]:
    base = {
        "decision_id": "1",
        "case_no": "T",
        "unit_note": "원",
        "transcriber": "test",
        "verified": "no",
        "serial_no": "1",
    }
    base.update(kw)
    return base


ROWS = [
    # rollover_off only (2023-04-09 is a Sunday), both roundings
    _row(
        flseq="10",
        table_label="표 1",
        row_idx="1",
        deal_type="consignment",
        base_date_kind="sales_close_date",
        base_date="2023-02-28",
        paid_date="2023-05-02",
        principal_krw="14790",
        delay_days_in_table="23",
        interest_in_table="144",
        serial_no="1",
    ),
    # same table, second supplier
    _row(
        flseq="10",
        table_label="표 1",
        row_idx="2",
        deal_type="consignment",
        base_date_kind="sales_close_date",
        base_date="2022-09-30",
        paid_date="2022-11-10",
        principal_krw="1813790",
        delay_days_in_table="1",
        interest_in_table="770",
        serial_no="2",
    ),
    # no base date: abstain, and check arithmetic from the table's own due date
    _row(
        flseq="20",
        table_label="표 2",
        row_idx="1",
        deal_type="direct",
        due_date_in_table="2024-05-20",
        paid_date="2024-06-07",
        principal_krw="1000000",
        delay_days_in_table="18",
        interest_in_table="7645",
    ),
    # 천원 table, due column = 기산일 (due + 1), 2024-09-29 is a Sunday -> rollover_on
    _row(
        decision_id="2",
        flseq="30",
        table_label="표 3",
        row_idx="1",
        deal_type="subcontract",
        base_date_kind="object_received_date",
        base_date="2024-07-31",
        due_date_in_table="2024-10-01",
        paid_date="2024-10-15",
        principal_krw="125125",
        delay_days_in_table="15",
        interest_in_table="797",
        unit_note="천원",
        notes="UNCERTAIN: test",
    ),
    # range row: due dates of both ends only match with rollover (추석 2023, weekend)
    _row(
        flseq="40",
        table_label="표 4",
        row_idx="1",
        deal_type="direct",
        base_date_kind="goods_received_date",
        base_date="2023-08-01~2023-08-15",
        due_date_in_table="2023-10-04~2023-10-16",
        principal_krw="1980000",
        delay_days_in_table="20",
        interest_in_table="16816",
    ),
    # range row before the registered direct-purchase rule version -> not computable
    _row(
        flseq="40",
        table_label="표 4",
        row_idx="2",
        deal_type="direct",
        base_date_kind="goods_received_date",
        base_date="2021-05-01~2021-05-15",
        due_date_in_table="2021-06-30~2021-07-14",
        principal_krw="1",
        delay_days_in_table="90~91",
        interest_in_table="1",
    ),
]


def _table(**kw: str) -> dict[str, str]:
    t = dict(TABLE_DEFAULTS)
    t.update(
        {
            "decision_id": "1",
            "case_no": "T",
            "unit_note": "원",
            "serial_unit": "supplier",
            "due_col_semantics": "due_date",
            "rows_elided": "no",
        }
    )
    t.update(kw)
    return t


TABLES = [
    _table(
        flseq="10",
        table_label="표 1",
        deal_type="consignment",
        caption="지연이자 내역",
        interest_col="지연이자",
        printed_total_interest="914",
    ),
    _table(flseq="20", table_label="표 2", deal_type="direct", rows_elided="yes"),
    _table(
        decision_id="2",
        flseq="30",
        table_label="표 3",
        deal_type="subcontract",
        unit_note="천원",
        due_col_semantics="delay_start",
        serial_unit="transaction",
    ),
    _table(flseq="40", table_label="표 4", deal_type="direct", rows_elided="yes"),
]


def _fact(kind: str, value: Any, refs: list[str], raw: str = "x", **kw: Any) -> dict[str, Any]:
    f = {
        "decision_id": "1",
        "kind": kind,
        "value": value,
        "raw": raw,
        "section": "이유",
        "char_start": 0,
        "char_end": len(raw),
        "confidence": "high",
        "masked": False,
        "table_refs": refs,
    }
    f.update(kw)
    return f


FACTS = [
    _fact("legal_interest_rate", "15.5", []),
    _fact("total_unpaid_interest", 1013, ["표 1"], raw="1,013원"),
    _fact("total_unpaid_interest", 1013, ["표 1"], raw="1,013원", char_start=50),  # repeated
    _fact("rounding_note", "x", [], raw="양자 간에는 '99원’의 차이가 있다."),
    _fact("supplier_count", 2, ["표 1"]),
    _fact("supplier_count", 9, ["표 4"]),  # rows elided -> compare with last 연번
]


@pytest.fixture()
def report(tmp_path: Path) -> dict[str, Any]:
    rows = _write_csv(tmp_path / "rows.csv", fe.ROW_COLUMNS, ROWS)
    tables = _write_csv(tmp_path / "tables.csv", fe.TABLE_COLUMNS, TABLES)
    facts = tmp_path / "facts.jsonl"
    facts.write_text("\n".join(json.dumps(f, ensure_ascii=False) for f in FACTS), encoding="utf-8")
    return fe.run(rows, tables, facts)


def _row_result(rep: dict[str, Any], key: str) -> dict[str, Any]:
    return next(r for r in rep["rows"] if r["key"] == key)


def test_variant_matching(report: dict[str, Any]) -> None:
    r1 = _row_result(report, "1/10/1")
    assert r1["mode"] == "e2"
    # the table has no due-date column for this row: 2 of 3 checks are evaluable, so the
    # row matches "all available" but can never match "all three"
    assert r1["evaluable_checks"] == 2
    assert r1["matched_all_available"] == ["rollover_off/floor", "rollover_off/half_up"]
    assert r1["matched_all_three"] == []
    sub = _row_result(report, "2/30/1")
    assert sub["evaluable_checks"] == 3
    assert sub["matched_all_three"] == ["rollover_on/floor", "rollover_on/half_up"]
    assert sub["matched_all_available"] == sub["matched_all_three"]
    on = next(v for v in sub["variants"] if v["label"] == "rollover_on/floor")
    assert on["due"] == "2024-09-30" and on["delay_days"] == 15
    assert on["interest_krw"] == 797_029  # within ±1,000 of 797 천원


def test_e2_summary(report: dict[str, Any]) -> None:
    e2 = report["E2"]["all_rows"]
    assert e2["rows"] == 3
    off = e2["per_variant"]["rollover_off/floor"]
    assert off["all_available_ok"] == {"n": 2, "of": 3, "rate": 0.6667}
    assert off["all_three_ok"] == {"n": 0, "of": 3, "rate": 0.0}
    assert off["all_three_ok_of_fully_evaluable"] == {"n": 0, "of": 1, "rate": 0.0}
    assert off["fully_evaluable"] == {"n": 1, "of": 3, "rate": 0.3333}
    on = e2["per_variant"]["rollover_on/floor"]
    assert on["all_three_ok"] == {"n": 1, "of": 3, "rate": 0.3333}
    assert on["due_ok"] == {"n": 1, "of": 1, "rate": 1.0}  # only evaluable rows
    post = e2["post_hoc_any_variant"]
    assert post["all_three_ok"] == {"n": 1, "of": 3, "rate": 0.3333}
    assert post["all_available_ok"]["n"] == 3
    assert e2["evaluable_checks_per_row"] == {"0": 0, "1": 0, "2": 2, "3": 1}
    assert e2["evaluable_rows_per_check"] == {"due_ok": 1, "delay_ok": 3, "interest_ok": 3}
    assert e2["abstained"] == {"n": 0, "of": 3, "rate": 0.0}
    assert e2["engine_full_computation"] == {"n": 3, "of": 3, "rate": 1.0}
    certain = report["E2"]["certain_rows_only"]
    assert certain["rows"] == 2  # the UNCERTAIN row is excluded


def test_no_base_date_abstains_and_conditional_arithmetic(report: dict[str, Any]) -> None:
    r = _row_result(report, "1/20/1")
    assert r["mode"] == "e2_cond"
    assert r["abstained"] is True
    assert "goods_received_date" in r["abstain_missing"]
    assert any("하차" in d for d in r["required_documents"])
    by = {v["label"]: v for v in r["variants"]}
    assert by["table_due/floor"]["delay_ok"] and by["table_due/half_up"]["delay_ok"]
    assert by["table_due/floor"]["interest_ok"] is False  # 7,643 vs 7,645 (tolerance 1)
    assert by["table_due/half_up"]["interest_ok"] is True


def test_e3_separates_missing_rule_versions(report: dict[str, Any]) -> None:
    e3 = report["E3"]
    assert e3["rows_without_base_date"] == 1
    assert e3["abstained_correctly"]["n"] == 1
    assert e3["abstained_no_rule_version"]["n"] == 1
    assert e3["no_rule_version_rows"] == ["1/40/2"]
    assert e3["false_abstentions"]["n"] == 0


def test_range_rows(report: dict[str, Any]) -> None:
    rng = report["E2"]["range_rows"]
    assert rng["rows"] == 2 and rng["not_computed_no_rule_version"] == 1
    assert rng["rollover_on"]["n"] == 1 and rng["rollover_off"]["n"] == 0
    assert rng["post_hoc_any_variant"]["n"] == 1


def test_e1_checks(report: dict[str, Any]) -> None:
    checks = report["E1"]["checks"]
    unpaid = [c for c in checks if c["fact_kind"] == "total_unpaid_interest"]
    assert len(unpaid) == 1 and unpaid[0]["mentions"] == 2  # duplicates merged
    c = unpaid[0]
    # single interest column -> compared with its printed total (914); text says 1,013
    assert c["printed_table_total_krw"] == 914
    assert c["text_minus_printed_krw"] == 99
    assert c["difference_stated_in_text"]["value"] == "x"
    # complete table: engine sums over both rows, per variant
    assert c["engine_sum_by_variant_krw"]["rollover_off/floor"] == 144 + 770
    counts = {(c["fact_kind"], c["text_value"]): c for c in checks if "equal" in c}
    assert counts[("supplier_count", 2)]["transcribed_distinct_serials"] == 2
    assert counts[("supplier_count", 2)]["equal"] is True
    assert counts[("supplier_count", 9)]["last_serial_no"] == 1
    assert counts[("supplier_count", 9)]["equal"] is False
    qa = report["E1"]["transcription_qa"]
    assert {
        "decision_id": "1",
        "table_label": "표 1",
        "interest_in_table": {"sum": 914, "printed": 914, "diff": 0},
    } in qa


def test_interest_tolerance() -> None:
    assert fe._interest_ok(797_029, 797, 1000) is True
    assert fe._interest_ok(1_031_034, 1030, 1000) is False
    assert fe._interest_ok(7_644, 7_643, 1) is True
    assert fe._interest_ok(7_645, 7_643, 1) is False
    assert fe._interest_ok(None, 1, 1) is None


def test_markdown_block_is_replaced_not_duplicated(tmp_path: Path, report: dict[str, Any]) -> None:
    md = tmp_path / "eval_results.md"
    md.write_text("# Evaluation results\n\n## BPI\nkeep me\n", encoding="utf-8")
    fe.update_markdown(md, fe.render_markdown(report))
    fe.update_markdown(md, fe.render_markdown(report))
    text = md.read_text(encoding="utf-8")
    assert text.count(fe.MD_START) == 1 and "keep me" in text
    assert "| rollover_off/floor | " in text
    assert "제시한 계산 중 정답을 포함한 비율" in text and "post hoc" in text
    assert "| any variant |" not in text  # the old, misleading row label is gone
    assert "verified=no" in text


def test_write_outputs(
    tmp_path: Path, report: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JETTAE_DATA_DIR", str(tmp_path / "data"))
    out, md = fe.write_outputs(report, docs=tmp_path)
    assert json.loads(out.read_text(encoding="utf-8"))["eval"] == "ftc"
    assert md.exists()


def test_missing_columns_rejected(tmp_path: Path) -> None:
    p = _write_csv(tmp_path / "bad.csv", ("decision_id", "flseq"), [{"decision_id": "1"}])
    with pytest.raises(ValueError, match="missing columns"):
        fe.load_rows(p)


def test_eval_cli_mounts_optional_modules() -> None:
    from typer.testing import CliRunner

    from jettae.evals import cli

    assert set(cli.EVAL_STATUS) == {m for m, _ in cli.OPTIONAL_EVALS}
    res = CliRunner().invoke(cli.app, ["--help"])
    assert res.exit_code == 0 and "ftc" in res.output


# ----------------------------------------------------------------------------- metric units
def _vc(**kw: Any) -> VariantCheck:
    base: dict[str, Any] = dict(
        label="v",
        due=date(2025, 10, 6),
        delay_days=14,
        interest=100,
        due_ok=True,
        delay_ok=True,
        interest_ok=True,
    )
    base.update(kw)
    return VariantCheck(**base)


@pytest.mark.parametrize(
    "checks,evaluated,all_three,all_available",
    [
        ((True, True, True), 3, True, True),
        ((True, True, None), 2, False, True),  # uncomputed interest never passes "all three"
        ((True, False, None), 2, False, False),
        ((None, None, None), 0, False, False),
        ((True, True, False), 3, False, False),
    ],
)
def test_variant_check_definitions(
    checks: tuple[bool | None, ...], evaluated: int, all_three: bool, all_available: bool
) -> None:
    v = _vc(due_ok=checks[0], delay_ok=checks[1], interest_ok=checks[2])
    assert v.evaluated == evaluated
    assert v.all_ok is all_three and v.all_available_ok is all_available
    assert v.fully_evaluated is (evaluated == 3)


def test_fully_computed_needs_all_three_engine_values() -> None:
    assert _vc().fully_computed
    assert not _vc(interest=None, interest_ok=None).fully_computed


def test_abstained_rows_count_against_all_three_and_full_computation() -> None:
    abst = RowResult("1/1/1", "1", "1", "표", "e2", False, abstained=True, rule_missing=True)
    ok = RowResult(
        "1/1/2",
        "1",
        "1",
        "표",
        "e2",
        False,
        abstained=False,
        variants=[_vc(label=variant_label(r, m)) for r, m in fe.VARIANTS],
    )
    part = RowResult(
        "1/1/3",
        "1",
        "1",
        "표",
        "e2",
        False,
        abstained=False,
        variants=[
            _vc(label=variant_label(r, m), interest=None, interest_ok=None) for r, m in fe.VARIANTS
        ],
    )
    e2 = summarize_e2([abst, ok, part])
    assert e2["abstained"] == {"n": 1, "of": 3, "rate": 0.3333}
    assert e2["engine_full_computation"]["n"] == 1
    assert e2["evaluable_checks_per_row"] == {"0": 1, "1": 0, "2": 1, "3": 1}
    per = e2["per_variant"]["rollover_off/floor"]
    assert per["all_three_ok"] == {"n": 1, "of": 3, "rate": 0.3333}
    assert per["all_available_ok"] == {"n": 2, "of": 2, "rate": 1.0}
    assert per["interest_ok"] == {"n": 1, "of": 1, "rate": 1.0}


def test_facade_keeps_the_public_import_paths() -> None:
    from jettae.evals import ftc_aggregate, ftc_inputs, ftc_report, ftc_rows

    assert fe.VariantCheck is ftc_rows.VariantCheck
    assert fe.load_rows is ftc_inputs.load_rows
    assert fe.render_markdown is ftc_report.render_markdown
    assert fe.summarize_e2 is ftc_aggregate.summarize_e2
    for mod in ("ftc_eval", "ftc_inputs", "ftc_rows", "ftc_aggregate", "ftc_report"):
        p = Path(fe.__file__).with_name(f"{mod}.py")
        assert len(p.read_text(encoding="utf-8").splitlines()) < 500, mod


def test_eval_ftc_cli_on_the_real_seed_files(tmp_path: Path) -> None:
    """``jettae eval ftc`` end to end on the tracked seed files, outputs redirected."""
    from typer.testing import CliRunner

    from jettae.evals import cli

    out, md = tmp_path / "f.json", tmp_path / "f.md"
    res = CliRunner().invoke(cli.app, ["ftc", "--out", str(out), "--md", str(md)])
    if res.exit_code == 1 and "입력 파일 없음" in res.output:
        pytest.skip("seed files not present")
    assert res.exit_code == 0, res.output
    rep = json.loads(out.read_text(encoding="utf-8"))
    e2 = rep["E2"]["all_rows"]
    for per in e2["per_variant"].values():
        assert per["all_three_ok"]["n"] <= per["all_available_ok"]["n"]
        assert per["all_three_ok"]["of"] == e2["rows"]
    assert sum(e2["evaluable_checks_per_row"].values()) == e2["rows"]
    assert "post-hoc any variant" in res.output
    assert "제시한 계산 중 정답을 포함한 비율" in md.read_text(encoding="utf-8")
