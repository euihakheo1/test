"""Money in integer minor units (KRW: 1 won). Never float."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from enum import StrEnum

from jettae.domain.errors import CurrencyMismatchError, InvalidMoneyError


class RoundingMode(StrEnum):
    """Explicit rounding mode for rate math (원 단위 처리)."""

    FLOOR = "floor"  # 절사 (toward -infinity; same as truncation for non-negative amounts)
    HALF_UP = "half_up"  # 반올림 (0.5 away from zero)


_DECIMAL_ROUNDING = {RoundingMode.FLOOR: ROUND_FLOOR, RoundingMode.HALF_UP: ROUND_HALF_UP}


def _to_decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, bool | float):
        raise InvalidMoneyError("float/bool are not allowed in money math; use Decimal or str")
    if isinstance(value, Decimal):
        return value
    return Decimal(value)


def round_to_int(value: Decimal, mode: RoundingMode | str) -> int:
    """Round a Decimal to an integer with an explicit rounding mode."""
    if not isinstance(value, Decimal):
        raise InvalidMoneyError(f"expected Decimal, got {type(value).__name__}")
    return int(value.quantize(Decimal(1), rounding=_DECIMAL_ROUNDING[RoundingMode(mode)]))


@dataclass(frozen=True, slots=True)
class Money:
    """An amount in integer minor units of ``currency``."""

    amount: int
    currency: str = "KRW"

    def __post_init__(self) -> None:
        if isinstance(self.amount, bool) or not isinstance(self.amount, int):
            raise InvalidMoneyError(
                f"Money.amount must be int minor units, got {type(self.amount).__name__}"
            )
        if not (
            isinstance(self.currency, str)
            and len(self.currency) == 3
            and self.currency.isascii()
            and self.currency.isalpha()
            and self.currency.isupper()
        ):
            raise InvalidMoneyError(f"invalid currency code: {self.currency!r}")

    # -- constructors -------------------------------------------------
    @classmethod
    def zero(cls, currency: str = "KRW") -> Money:
        return cls(0, currency)

    @classmethod
    def from_decimal(
        cls, value: Decimal | int | str, mode: RoundingMode | str, currency: str = "KRW"
    ) -> Money:
        return cls(round_to_int(_to_decimal(value), mode), currency)

    # -- helpers ------------------------------------------------------
    def _check(self, other: object) -> Money:
        if not isinstance(other, Money):
            raise TypeError(f"expected Money, got {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatchError(f"{self.currency} vs {other.currency}")
        return other

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    # -- arithmetic ---------------------------------------------------
    def __add__(self, other: Money) -> Money:
        o = self._check(other)
        return Money(self.amount + o.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        o = self._check(other)
        return Money(self.amount - o.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    def __mul__(self, factor: int) -> Money:
        if isinstance(factor, bool) or not isinstance(factor, int):
            raise TypeError("Money can only be multiplied by int; use apply_rate for rates")
        return Money(self.amount * factor, self.currency)

    __rmul__ = __mul__

    def apply_rate(self, rate: Decimal | int | str, mode: RoundingMode | str) -> Money:
        """``self x rate`` rounded with ``mode``. ``rate`` must be Decimal/int/str (no float)."""
        return Money.from_decimal(Decimal(self.amount) * _to_decimal(rate), mode, self.currency)

    # -- comparisons (same currency only) ------------------------------
    def __lt__(self, other: Money) -> bool:
        return self.amount < self._check(other).amount

    def __le__(self, other: Money) -> bool:
        return self.amount <= self._check(other).amount

    def __gt__(self, other: Money) -> bool:
        return self.amount > self._check(other).amount

    def __ge__(self, other: Money) -> bool:
        return self.amount >= self._check(other).amount

    def __str__(self) -> str:
        if self.currency == "KRW":
            return f"{self.amount:,}원"
        return f"{self.amount:,} {self.currency} (minor units)"


def sum_money(items: Iterable[Money], currency: str = "KRW") -> Money:
    total = Money.zero(currency)
    for m in items:
        total = total + m
    return total
