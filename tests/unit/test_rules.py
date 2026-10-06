import json
from datetime import date

import pytest

from jettae.domain import Money, RoundingMode, TradeType
from jettae.rules import (
    DueResult,
    Insufficient,
    KrCalendar,
    RuleRegistry,
    compute_due,
    default_registry,
)
from jettae.rules.kr_retail import (
    RULE_CONSIGNMENT,
    RULE_DIRECT,
    compute_interest,
)

CAL = KrCalendar()


def due(*a, **kw) -> DueResult:
    r = compute_due(*a, calendar=CAL, **kw)
    assert isinstance(r, DueResult), r
    return r


# ---------------------------------------------------------------- calendar
def test_calendar_knows_korean_holidays():
    assert CAL.is_holiday(date(2025, 10, 6))  # 추석
    assert CAL.is_holiday(date(2025, 3, 3))  # 삼일절 대체공휴일
    assert CAL.is_holiday(date(2025, 1, 27))  # 임시공휴일 (holidays.KR)
    assert not CAL.is_business_day(date(2025, 6, 1))  # Sunday
    assert CAL.next_business_day(date(2025, 10, 3)) == date(2025, 10, 10)


def test_holiday_override_file(tmp_path):
    p = tmp_path / "ov.json"
    p.write_text(
        json.dumps({"add": [{"date": "2025-03-11", "name": "임시공휴일(테스트)"}], "remove": []}),
        encoding="utf-8",
    )
    cal = KrCalendar.from_override_file(p)
    assert cal.is_holiday(date(2025, 3, 11))
    assert cal.fingerprint() != CAL.fingerprint()
    r = compute_due(TradeType.DIRECT, date(2025, 1, 10), Money(1), rollover=True, calendar=cal)
    assert isinstance(r, DueResult)
    assert r.due_date == date(2025, 3, 12)
    p2 = tmp_path / "rm.json"
    p2.write_text(json.dumps({"remove": ["2025-10-06"]}), encoding="utf-8")
    assert not KrCalendar.from_override_file(p2).is_holiday(date(2025, 10, 6))
    with pytest.raises(FileNotFoundError):
        KrCalendar.from_override_file(tmp_path / "missing.json")


# ---------------------------------------------------------------- art.8 direct purchase
def test_direct_purchase_business_day_due_single_variant():
    r = due(TradeType.DIRECT, date(2025, 1, 10), Money(1_000_000), paid_date=date(2025, 3, 11))
    assert r.due_date == date(2025, 3, 11)
    assert r.delay_days == 0
    assert r.interest == Money(0)
    assert len(r.variants) == 1 and r.unresolved == ()
    assert r.rule_version == f"{RULE_DIRECT}@2021-10-21"


def test_due_on_sunday_returns_both_variants_when_rollover_unresolved():
    # 2025-04-02 + 60 = 2025-06-01 (Sunday); next business day 2025-06-02 (Mon)
    r = due(TradeType.DIRECT, date(2025, 4, 2), Money(10_000_000), paid_date=date(2025, 6, 12))
    labels = {v.label: v for v in r.variants}
    assert labels["rollover_off"].due_date == date(2025, 6, 1)
    assert labels["rollover_on"].due_date == date(2025, 6, 2)
    assert labels["rollover_off"].delay_days == 11
    assert labels["rollover_on"].delay_days == 10
    assert "rollover" in r.unresolved
    # 10,000,000 x 0.155 x 11 / 365 = 46712.32...
    assert labels["rollover_off"].interest == Money(46712)


def test_due_on_korean_holiday_with_rollover_and_rounding():
    # 2025-08-07 + 60 = 2025-10-06 (추석); 10/7 연휴, 10/8 대체공휴일, 10/9 한글날 -> 10/10
    paid = date(2025, 10, 20)
    off = due(TradeType.DIRECT, date(2025, 8, 7), Money(10_000_000), paid_date=paid, rollover=False)
    on = due(TradeType.DIRECT, date(2025, 8, 7), Money(10_000_000), paid_date=paid, rollover=True)
    assert off.due_date == date(2025, 10, 6) and off.delay_days == 14
    assert on.due_date == date(2025, 10, 10) and on.delay_days == 10
    assert off.interest == Money(59452)  # 59452.05 floor
    assert on.interest == Money(42465)  # 42465.75 floor
    on_half = due(
        TradeType.DIRECT,
        date(2025, 8, 7),
        Money(10_000_000),
        paid_date=paid,
        rollover=True,
        rounding=RoundingMode.HALF_UP,
    )
    assert on_half.interest == Money(42466)
    assert on_half.rounding is RoundingMode.HALF_UP


def test_consignment_month_close_plus_40():
    r = due(TradeType.CONSIGNMENT, date(2025, 1, 31), Money(5_000_000), paid_date=date(2025, 3, 20))
    assert r.due_date == date(2025, 3, 12)
    assert r.delay_days == 8
    assert r.interest == Money(16986)  # 16986.30
    assert r.rule_version.startswith(RULE_CONSIGNMENT)


def test_subcontract_art13_and_unpaid_as_of():
    r = due(TradeType.SUBCONTRACT, date(2025, 1, 10), Money(1_000_000), as_of=date(2025, 4, 10))
    assert r.due_date == date(2025, 3, 11)
    assert r.delay_days == 30
    assert r.interest == Money(12739)
    assert r.interest_rule_version == "kr.subcontract.delay_interest@2018-21"


def test_interest_formula():
    assert compute_interest(
        Money(1_000_000), __import__("decimal").Decimal("0.155"), 0, 365, RoundingMode.FLOOR
    ) == Money(0)


# ---------------------------------------------------------------- insufficient evidence
def test_missing_base_date_is_insufficient_and_tax_invoice_not_substituted():
    r = compute_due(
        TradeType.DIRECT, None, Money(1_000_000), tax_invoice_date=date(2025, 1, 5), calendar=CAL
    )
    assert isinstance(r, Insufficient)
    assert r.missing == ("goods_received_date",)
    assert any("입고" in d for d in r.required_documents)
    assert any("세금계산서" in n and "사용하지 않음" in n for n in r.notes)
    r2 = compute_due(TradeType.CONSIGNMENT, None, Money(1), calendar=CAL)
    assert isinstance(r2, Insufficient) and r2.missing == ("sales_close_date",)
    r3 = compute_due(None, date(2025, 1, 1), Money(1), calendar=CAL)
    assert isinstance(r3, Insufficient) and r3.missing == ("trade_type",)
    r4 = compute_due(TradeType.SUBCONTRACT, None, Money(1), calendar=CAL)
    assert isinstance(r4, Insufficient) and r4.missing == ("object_received_date",)


def test_base_date_before_any_registered_version():
    r = compute_due(TradeType.DIRECT, date(2020, 1, 1), Money(1), calendar=CAL)
    assert isinstance(r, Insufficient) and r.missing == ("rule_version",)


# ---------------------------------------------------------------- registry & amendment
def test_amendment_registered_inactive():
    reg = default_registry()
    amend = reg.get(RULE_DIRECT, "2026-amendment")
    assert amend.effective_from is None and not amend.active
    assert reg.resolve(RULE_DIRECT, date(2027, 6, 1)).version == "2021-10-21"
    r = due(TradeType.DIRECT, date(2027, 1, 5), Money(1), rollover=False, registry=reg)
    assert r.term_days == 60


def test_amendment_applies_only_after_activation_date():
    reg = default_registry().activate(RULE_DIRECT, "2026-amendment", date(2027, 1, 1))
    before = due(TradeType.DIRECT, date(2026, 12, 31), Money(1), rollover=False, registry=reg)
    after = due(TradeType.DIRECT, date(2027, 1, 5), Money(1), rollover=False, registry=reg)
    assert before.term_days == 60
    assert after.term_days == 35 and after.due_date == date(2027, 2, 9)
    monthly = due(
        TradeType.DIRECT,
        date(2027, 1, 5),
        Money(1),
        rollover=False,
        registry=reg,
        monthly_settlement=True,
    )
    assert monthly.term_days == 20 and monthly.due_date == date(2027, 2, 20)
    # known-time query: before the amendment was known, the old version applies
    old_view = due(
        TradeType.DIRECT,
        date(2027, 1, 5),
        Money(1),
        rollover=False,
        registry=reg,
        known_at=date(2026, 9, 1),
    )
    assert old_view.term_days == 60
    cons = reg.activate(RULE_CONSIGNMENT, "2026-amendment", date(2027, 1, 1))
    assert cons.resolve(RULE_CONSIGNMENT, date(2027, 3, 1)).params["term_days"] == 20


def test_registry_rejects_duplicates_and_fingerprint_changes():
    reg = default_registry()
    rv = reg.get(RULE_DIRECT, "2021-10-21")
    with pytest.raises(ValueError):
        RuleRegistry([rv, rv])
    act = reg.activate(RULE_DIRECT, "2026-amendment", date(2027, 1, 1))
    assert act.fingerprint() != reg.fingerprint()
    assert all(v.source_url.startswith("https://www.law.go.kr/") for v in reg)
