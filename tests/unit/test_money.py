from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from jettae.domain import (
    CurrencyMismatchError,
    InvalidMoneyError,
    Money,
    RoundingMode,
    content_hash,
    round_to_int,
    sum_money,
)
from jettae.domain.dates import add_days, ensure_utc, month_end, next_business_day, to_display


def test_money_is_integer_minor_units():
    assert Money(1000).amount == 1000
    with pytest.raises(InvalidMoneyError):
        Money(10.5)  # type: ignore[arg-type]
    with pytest.raises(InvalidMoneyError):
        Money(True)  # type: ignore[arg-type]
    with pytest.raises(InvalidMoneyError):
        Money(1, "krw")


def test_same_currency_arithmetic_only():
    assert Money(1000) + Money(250) == Money(1250)
    assert Money(1000) - Money(1250) == Money(-250)
    assert -Money(5) == Money(-5)
    assert Money(3) * 4 == Money(12)
    assert sum_money([Money(1), Money(2), Money(3)]) == Money(6)
    with pytest.raises(CurrencyMismatchError):
        Money(1) + Money(1, "USD")
    with pytest.raises(CurrencyMismatchError):
        _ = Money(1) < Money(2, "USD")
    with pytest.raises(TypeError):
        Money(1) * Decimal("1.5")  # type: ignore[operator]


def test_rounding_modes_are_explicit():
    assert round_to_int(Decimal("42465.75"), RoundingMode.FLOOR) == 42465
    assert round_to_int(Decimal("42465.75"), RoundingMode.HALF_UP) == 42466
    assert round_to_int(Decimal("0.5"), RoundingMode.HALF_UP) == 1
    assert round_to_int(Decimal("0.4999"), RoundingMode.HALF_UP) == 0
    assert round_to_int(Decimal("-0.5"), RoundingMode.FLOOR) == -1


def test_apply_rate_rejects_float():
    assert Money(10_000_000).apply_rate("0.155", RoundingMode.FLOOR) == Money(1_550_000)
    assert Money(333).apply_rate(Decimal("0.5"), RoundingMode.HALF_UP) == Money(167)
    assert Money(333).apply_rate(Decimal("0.5"), RoundingMode.FLOOR) == Money(166)
    with pytest.raises(InvalidMoneyError):
        Money(1).apply_rate(0.1, RoundingMode.FLOOR)  # type: ignore[arg-type]


def test_canonical_hash_rejects_float_and_is_stable():
    assert content_hash({"a": Money(1), "b": date(2025, 1, 1)}) == content_hash(
        {"b": date(2025, 1, 1), "a": Money(1)}
    )
    with pytest.raises(TypeError):
        content_hash({"x": 1.0})


def test_date_helpers():
    # 민법 제157조 초일 불산입: 1/10 + 60일 = 3/11
    assert add_days(date(2025, 1, 10), 60) == date(2025, 3, 11)
    assert month_end(date(2024, 2, 3)) == date(2024, 2, 29)
    assert next_business_day(date(2025, 5, 3), lambda d: False) == date(2025, 5, 5)
    with pytest.raises(ValueError):
        ensure_utc(datetime(2025, 1, 1))
    assert to_display(datetime(2025, 1, 1, 15, tzinfo=UTC)).date() == date(2025, 1, 2)
