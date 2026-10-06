import json
from datetime import date

from jt_unit_helpers import inv, snapshot, txn
from typer.testing import CliRunner

from jettae.app import render_explanation
from jettae.cli import MODULE_STATUS, app
from jettae.domain import Allocation, Money, SourceSpan
from jettae.evidence import full_recompute
from jettae.verify import (
    check_allocations,
    check_citation,
    check_explanation,
    check_numbers,
    check_wording,
)


def test_citation_check():
    span = SourceSpan("d1", {"page": 1}, "지급  기한은\n60일")
    assert check_citation(span, "... 지급 기한은 60일 이내 ...").passed
    assert not check_citation(span, "다른 내용").passed
    assert not check_citation(span, None).passed


def test_numbers_check_against_engine_output():
    d = full_recompute(
        snapshot(
            invoices=[inv("I-001", 10_000_000)], txns=[txn("T1", 10_000_000, date(2025, 10, 20))]
        )
    ).decisions["dec:I-001"]
    text = render_explanation(d)
    assert check_explanation(text, d).passed
    tampered = text.replace("59,452원", "59,999원")
    res = check_explanation(tampered, d)
    assert not res.passed and any("59999" in x for x in res.details)
    assert not check_explanation(text + "\n지급기한 2025-10-07", d).passed
    assert check_numbers("합계 1,000원", [1000]).passed


def test_conservation_and_wording_checks():
    ok = [Allocation("P", "A", Money(100))]
    assert check_allocations(ok, {"P": Money(100)}, {"A": Money(100)}).passed
    bad = [Allocation("P", "A", Money(100)), Allocation("P", "B", Money(50))]
    assert not check_allocations(bad, {"P": Money(100)}, {"A": Money(100), "B": Money(50)}).passed
    assert not check_wording("이 거래는 위법입니다").passed
    assert not check_wording("지연이자를 받을 수 있습니다").passed
    assert check_wording("차이 59,452원, 필요 서류: 입고 기록").passed


runner = CliRunner()


def test_cli_rules_due_variants_json():
    r = runner.invoke(
        app,
        [
            "rules",
            "due",
            "--type",
            "direct",
            "--base",
            "2025-08-07",
            "--paid",
            "2025-10-20",
            "--amount",
            "10000000",
            "--rounding",
            "half_up",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert [v["interest"]["$money"][0] for v in data["variants"]] == [59452, 42466]
    assert data["unresolved"] == ["rollover"]


def test_cli_rules_due_table_and_insufficient():
    r = runner.invoke(
        app,
        [
            "rules",
            "due",
            "--type",
            "consignment",
            "--base",
            "2025-01-31",
            "--paid",
            "2025-03-20",
            "--amount",
            "5000000",
            "--rollover",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "2025-03-12" in r.output and "16,986원" in r.output and "law.go.kr" in r.output
    r2 = runner.invoke(
        app,
        ["rules", "due", "--type", "direct", "--amount", "1", "--tax-invoice-date", "2025-01-05"],
    )
    assert r2.exit_code == 2
    assert "근거 부족" in r2.output


def test_cli_lazy_registration_skips_missing_modules():
    r = runner.invoke(app, ["modules"])
    assert r.exit_code == 0
    assert "jettae.worker" in MODULE_STATUS
    assert runner.invoke(app, ["rules", "list"]).exit_code == 0
