"""E6 machinery on hand-written fixtures (the real run uses data/seeds + BPI only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jettae.domain.status import ChangeKind
from jettae.evals import recompute_eval as e6
from jettae.sources import upsert_md_section

FIX_XES = Path(__file__).parent / "fixtures" / "bpi_tiny.xes"

CSV = """사건번호,표번호,행번호,납품업자,상품수령일,지급일,금액,거래형태,정정
D1,1,1,가나상사,2024-01-05,2024-03-20,"1,000,000",직매입,
D1,1,2,가나상사,2024.01.10,2024-03-01,500000,직매입,
D1,1,3,다라유통,20240201,2024-05-02,"2,500,000원",직매입,
D1,1,4,다라유통,2024-02-01,,700000,직매입,
D1,1,5,다라유통,2024-02-03,2024-06-01,2500000,직매입,3
D2,1,1,마바,2024-03-01,2024-04-01,abc,직매입,
"""


@pytest.fixture()
def data_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JETTAE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JETTAE_REPO_ROOT", str(tmp_path))
    return tmp_path


def test_column_mapping_korean_headers() -> None:
    cols = e6.map_columns(["사건번호", "행번호", "상품수령일", "지급일", "금액 ", "거래형태"])
    assert cols["case"] == "사건번호" and cols["amount"] == "금액 " and cols["paid"] == "지급일"


def test_ftc_scenario_and_equality(tmp_path: Path) -> None:
    p = tmp_path / "rows.csv"
    p.write_text(CSV, encoding="utf-8-sig")
    sc = e6.ftc_scenario(p)
    assert sc.provenance["rows"] == 6 and sc.provenance["rows_skipped"] == 1
    assert len(sc.base.invoices) == 4
    kinds = [(c.kind, c.entity) for c in sc.changes]
    # 3 payments in date order, then the correction of row 3 as an update
    assert kinds == [(ChangeKind.ADD, "bank_txn")] * 3 + [(ChangeKind.UPDATE, "invoice")]
    assert [c.entity_id for c in sc.changes[:3]] == [
        "pay:ftc:D1:1:2",
        "pay:ftc:D1:1:1",
        "pay:ftc:D1:1:3",
    ]
    inv = {i.id: i for i in sc.base.invoices}["ftc:D1:1:1"]
    assert inv.amount.amount == 1_000_000 and inv.trade_type is not None
    res = e6.run_scenario(sc)
    assert res["all_equal"] and res["mismatching_steps"] == 0
    assert res["fallback_full_steps"] == 0
    # each payment touches one counterparty group of two
    assert res["recomputed_groups_total"] < res["full_groups_total"]
    assert all(s["recomputed_groups"] == 1 for s in res["steps"])


def test_ftc_scenario_unrecognised(tmp_path: Path) -> None:
    p = tmp_path / "rows.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(e6.ScenarioUnavailable):
        e6.ftc_scenario(p)
    with pytest.raises(e6.ScenarioUnavailable):
        e6.ftc_scenario(tmp_path / "missing.csv")


def test_bpi_scenario_equality() -> None:
    sc = e6.bpi_scenario(FIX_XES, max_items=10, max_changes=10)
    assert sc.provenance["po_items"] == 4  # items with invoice receipts
    assert [c.entity for c in sc.changes] == ["bank_txn", "bank_txn"]
    res = e6.run_scenario(sc)
    assert res["all_equal"] and res["changes"] == 2


def test_run_without_real_inputs_reports_not_run(data_tmp: Path) -> None:
    md = data_tmp / "docs" / "eval_results.md"
    res = e6.run(out=data_tmp / "r.json", md=md)
    assert res["scenarios"] == [] and res["all_equal"] is None
    assert {s["scenario"] for s in res["skipped"]} == {"ftc_rows", "bpi2019"}
    assert "Not run on real data" in md.read_text(encoding="utf-8")


def test_upsert_md_section_keeps_other_blocks(tmp_path: Path) -> None:
    p = tmp_path / "e.md"
    upsert_md_section(p, "E1", "## E1\nold")
    upsert_md_section(p, "E4", "## E4\nfirst")
    upsert_md_section(p, "E4", "## E4\nsecond")
    t = p.read_text(encoding="utf-8")
    assert t.startswith("# Evaluation results")
    assert "## E1\nold" in t and "second" in t and "first" not in t
    assert t.count("<!-- E4:start -->") == 1


def test_equality_check_has_teeth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale incremental result (nothing recomputed) must be reported as a mismatch."""
    p = tmp_path / "rows.csv"
    p.write_text(CSV, encoding="utf-8-sig")
    sc = e6.ftc_scenario(p)
    real = e6.incremental_recompute

    def stale(prev, snap, changes):  # type: ignore[no-untyped-def]
        _res, plan, _groups = real(prev, snap, changes)
        return prev, plan, frozenset()

    monkeypatch.setattr(e6, "incremental_recompute", stale)
    res = e6.run_scenario(sc)
    assert not res["all_equal"] and res["mismatching_steps"] == len(sc.changes)


SEED_HEADER = (
    "decision_id,case_no,flseq,table_label,row_idx,deal_type,base_date_kind,base_date,"
    "due_date_in_table,paid_date,principal_krw,verified,serial_no\n"
)
SEED_LAYOUT = (
    SEED_HEADER
    + """1,X,9,표 1,1,direct,,,2024-01-10,2024-01-20,1000,no,1
1,X,9,표 1,2,direct,,,2024-02-10,2024-02-11,2000,no,1
1,X,9,표 11,1,consignment,월판매마감일,2024-01-31,2024-03-11,2024-03-15,3000,no,1
1,X,9,표 11,2,consignment,월판매마감일,2024-02-29,2024-04-09,,4000,yes,2
"""
)


def test_ftc_scenario_seed_layout(tmp_path: Path) -> None:
    p = tmp_path / "ftc_rows.csv"
    p.write_text(SEED_LAYOUT, encoding="utf-8")
    sc = e6.ftc_scenario(p)
    inv = {i.id: i for i in sc.base.invoices}
    assert set(inv) == {"ftc:1:표1:1", "ftc:1:표1:2", "ftc:1:표11:1", "ftc:1:표11:2"}
    assert inv["ftc:1:표11:1"].sales_close_date is not None
    assert inv["ftc:1:표1:1"].goods_received_date is None  # no base date in the row: not invented
    # '표 1'/supplier 1 and '표 11'/supplier 1 must not collapse into one counterparty group
    assert len(sc.base.receivable_groups) == 3
    assert sc.provenance["rows_marked_unverified"] == 3
    assert sc.provenance["payment_changes"] == 3
    res = e6.run_scenario(sc)
    assert res["all_equal"]


def test_adhoc_input_never_touches_tracked_results(data_tmp: Path) -> None:
    p = data_tmp / "rows.csv"
    p.write_text(CSV, encoding="utf-8-sig")
    res = e6.run(ftc_rows=p)
    assert res["adhoc_inputs"] and res["kind"].startswith("AD-HOC INPUT")
    results = data_tmp / "data" / "results"
    assert (results / "recompute_eval_adhoc.json").exists()
    assert not (results / "recompute_eval.json").exists()
    assert not (data_tmp / "docs" / "eval_results.md").exists()
    assert "AD-HOC INPUT" in e6.render_md(res)
