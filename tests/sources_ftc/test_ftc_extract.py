"""Regex case-fact extraction on real decision text, with provenance checks."""

from __future__ import annotations

import pytest
from ftc_test_helpers import decision

from jettae.sources.ftc.extract import (
    CaseFact,
    extract_case_facts,
    parse_number,
    summarize,
)


def _facts(name: str) -> list[CaseFact]:
    return extract_case_facts(decision(name))


def _kind(facts: list[CaseFact], kind: str) -> list[CaseFact]:
    return [f for f in facts if f.kind == kind]


@pytest.mark.parametrize("name", ["f16947.xml", "x19065.xml", "x19005.xml", "x19299.xml"])
def test_every_fact_has_exact_provenance(name: str) -> None:
    d = decision(name)
    facts = extract_case_facts(d)
    assert facts
    for f in facts:
        assert d.sections[f.section][f.char_start : f.char_end] == f.raw
        span = f.span(f"ftc:{d.decision_id}")
        assert span.locator["decision_id"] == d.decision_id
        assert span.excerpt == f.raw


def test_homeplus_totals_rates_and_footnotes() -> None:
    facts = _facts("x19065.xml")
    unpaid = _kind(facts, "total_unpaid_interest")
    first = unpaid[0]
    # "나머지 연 7.5%에 해당하는 지연이자 총 401,177,688원 [각주] 을 지급하지 아니하였다"
    assert first.value == 401_177_688
    assert first.confidence == "high"
    assert first.table_refs == ("표 11", "표 12")
    assert 26_953_534 in {f.value for f in unpaid}
    legal = {f.value for f in _kind(facts, "legal_interest_rate")}
    other = {f.value for f in _kind(facts, "interest_rate_other")}
    assert legal == {"15.5"}
    assert {"8", "7.5"} <= other  # agreed 8% and the remaining 7.5% are not the legal rate
    assert 534 in {f.value for f in _kind(facts, "supplier_count")}
    terms = {(f.value["base"], f.value["days"]) for f in _kind(facts, "term_days")}
    assert {("상품수령일", 60), ("월판매마감일", 40)} <= terms
    rounding = _kind(facts, "rounding_note")
    assert rounding and rounding[0].in_footnote
    assert "절사" in rounding[0].value and "반올림" in rounding[0].value
    base_defs = [f for f in _kind(facts, "base_date_definition") if f.in_footnote]
    assert any("매입기간 종료일" in f.value for f in base_defs)


def test_emart_footnote_holiday_rule_and_table_links() -> None:
    facts = _facts("f16947.xml")
    fn8 = [f for f in facts if f.section == "각주:8"]
    assert any(
        f.kind == "base_date_definition" and "마지막날이 공휴일인 경우 그 익일" in f.value
        for f in fn8
    )
    # amounts link to the nearest preceding table mention, not to every table in the paragraph
    by_value = {f.value: f for f in facts if f.kind.startswith("total_")}
    assert by_value[39_043_331].kind == "total_interest"
    assert by_value[39_043_331].table_refs == ("표 5",)
    assert by_value[2_281_895].kind == "total_unpaid_interest"
    assert by_value[2_281_895].table_refs == ("표 6",)
    assert by_value[128_111_164].kind == "total_delayed_principal"


def test_masked_numbers_are_recorded_not_guessed() -> None:
    facts = _facts("x19005.xml")
    masked = [f for f in facts if f.masked]
    assert {f.kind for f in masked} >= {"supplier_count", "transaction_count"}
    assert all(f.value is None and "*" in f.raw for f in masked)
    rng = _kind(facts, "delay_days_range")
    assert rng[0].value == {"min": 1, "max": 233}
    assert rng[0].confidence == "high"
    defs = [f.value for f in _kind(facts, "base_date_definition") if f.in_footnote]
    assert any("상품하차일" in v for v in defs)


def test_total_of_which_unpaid_part() -> None:
    """'지연이자 총 908,892,108원 중 853,285,820원을 지급하지 아니한' (real 쿠팡 text)."""
    facts = _facts("x19005.xml")
    vals = {(f.kind, f.value) for f in facts if f.kind.startswith("total_")}
    assert ("total_interest", 908_892_108) in vals
    assert ("total_unpaid_interest", 853_285_820) in vals
    assert ("total_unpaid_interest", 908_892_108) not in vals


def test_thousand_won_unit_is_scaled() -> None:
    facts = _facts("x19299.xml")
    unpaid = _kind(facts, "total_unpaid_interest")
    assert unpaid[0].value == 3_564_000
    assert unpaid[0].unit_multiplier == 1000
    assert unpaid[0].raw == "3,564천 원"
    assert 6 in {f.value for f in _kind(facts, "supplier_count")}
    assert 7 in {f.value for f in _kind(facts, "transaction_count")}


@pytest.mark.parametrize(
    ("raw", "value"),
    [("1,234", 1234), ("401,177,688", 401177688), ("***,***", None), ("1*,000", None), ("", None)],
)
def test_parse_number(raw: str, value: int | None) -> None:
    assert parse_number(raw) == value


def test_summarize_counts() -> None:
    s = summarize(_facts("x19005.xml"))
    assert s["supplier_count"]["masked"] >= 1
    assert sum(v["n"] for v in s.values()) == len(_facts("x19005.xml"))
