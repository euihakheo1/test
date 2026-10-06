"""Contractual payment term (약정 기한) from an agreement's ``payment_term_days`` (pure).

This is NOT a statutory rule and is not in the rule registry:

- it never computes delay interest. The statutory delay rate (연 15.5%, 공정위 고시) applies to
  the statutory term only; a contractual rate, if any, is not extracted, so interest on the
  contractual due date is reported as "not computed" (``interest = None``);
- the agreement's counting start (기산점) is not extracted either. The computation assumes
  the statutory base date for the trade type (상품수령일 / 월 판매마감일) and reports that
  assumption as the unresolved condition ``contract_term_base``;
- no business-day rollover is applied (contracts word this differently); the result says
  whether the date is a business day.

The output is a separate date shown next to the statutory due date, with the difference in
days. It describes a difference only; it never replaces the statutory computation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from jettae.domain.dates import add_days, days_between
from jettae.domain.status import TradeType
from jettae.rules.calendar_kr import KrCalendar, default_calendar
from jettae.rules.kr_retail import BASE_FIELD, BASE_LABEL, REQUIRED_DOCS, Insufficient

CONTRACT_TERM_ID = "contract.payment_term_days"  # label, not a registered rule version
UNRESOLVED_BASE = "contract_term_base"
NO_INTEREST_NOTE = (
    "약정 기한에는 법정 지연이율(연 15.5%)을 적용하지 않음: 약정 기한 기준 지연이자 미계산"
)


@dataclass(frozen=True)
class ContractualDue:
    term_days: int
    base_field: str
    base_date: date
    due_date: date
    is_business_day: bool
    assumptions: tuple[str, ...]
    unresolved: tuple[str, ...] = (UNRESOLVED_BASE,)
    interest: None = None  # never computed (see module docstring)


def compute_contractual_due(
    term_days: int,
    trade_type: TradeType | str | None,
    base_date: date | None,
    *,
    calendar: KrCalendar | None = None,
) -> ContractualDue | Insufficient:
    """``base_date`` + ``term_days`` calendar days (초일 불산입), without rollover/interest."""
    if term_days < 0:
        raise ValueError("payment_term_days must not be negative")
    if trade_type is None:
        return Insufficient(("trade_type",), REQUIRED_DOCS["trade_type"])
    tt = TradeType(trade_type)
    field_name = BASE_FIELD[tt]
    if base_date is None:
        key = "object_received_date" if tt is TradeType.SUBCONTRACT else field_name
        return Insufficient(
            (key,),
            REQUIRED_DOCS[key],
            (f"{BASE_LABEL[tt]} 미확인: 약정 기한을 계산하지 않음",),
        )
    cal = calendar or default_calendar()
    due = add_days(base_date, term_days)
    business = cal.next_business_day(due) == due
    return ContractualDue(
        term_days=term_days,
        base_field=field_name,
        base_date=base_date,
        due_date=due,
        is_business_day=business,
        assumptions=(
            f"약정 기한 = {BASE_LABEL[tt]} {base_date.isoformat()} + 약정 {term_days}일"
            f" = {due.isoformat()} ({cal.describe(due)}, 초일 불산입)",
            "약정서의 기산점 미확인: 법정 기산일과 같은 기준일로 가정",
            "약정 기한 말일이 휴일인 경우의 이월은 적용하지 않음",
            NO_INTEREST_NOTE,
        ),
    )


def difference_days(contractual_due: date, statutory_due: date) -> int:
    """Contractual minus statutory due date, in days (negative = contract is earlier)."""
    return days_between(statutory_due, contractual_due)
