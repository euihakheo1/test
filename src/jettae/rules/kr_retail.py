"""Statutory payment-term rule pack (first rule pack, SPEC §4).

- 대규모유통업법 제8조: 직매입 = 상품수령일 + 60일, 특약매입 등 = 월 판매마감일 + 40일.
- 하도급법 제13조: 목적물 수령일 + 60일.
- 지연이자 = 미지급 원금 x 연 15.5% x 지연일수 / 365 (공정위 고시), rounding mode is a parameter.
- Rollover (기한 말일이 토·일·공휴일이면 다음 영업일) is a parameter; when it is unresolved and
  matters, both variants are returned with the unresolved condition.
- A missing base date yields :class:`Insufficient`; the tax-invoice date is never substituted.

Output wording: describes dates, differences, required documents and conditions only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from jettae.domain.dates import add_days, days_between, month_end
from jettae.domain.money import Money, RoundingMode
from jettae.domain.status import TradeType
from jettae.rules.calendar_kr import KrCalendar, default_calendar
from jettae.rules.registry import RuleRegistry, RuleVersion

# Assumption added when neither a payment date nor as_of ends the delay period.
END_UNSET_NOTE = "지급일·기준일(as_of) 미지정: 지연일수 0으로 표시"

RULE_DIRECT = "kr.large_retail.art8.direct_purchase"
RULE_CONSIGNMENT = "kr.large_retail.art8.consignment"
RULE_SUBCONTRACT = "kr.subcontract.art13"
RULE_INTEREST_RETAIL = "kr.large_retail.delay_interest"
RULE_INTEREST_SUBCONTRACT = "kr.subcontract.delay_interest"

URL_ART8 = "https://www.law.go.kr/법령/대규모유통업에서의거래공정화에관한법률/제8조"
URL_ART13 = "https://www.law.go.kr/법령/하도급거래공정화에관한법률/제13조"
URL_INTEREST_RETAIL = (
    "https://www.law.go.kr/DRF/lawService.do?OC=test&target=admrul&ID=2100000205723&type=HTML"
)
URL_INTEREST_SUBCONTRACT = (
    "https://www.law.go.kr/DRF/lawService.do?OC=test&target=admrul&ID=2100000171352&type=HTML"
)

TERM_RULE = {
    TradeType.DIRECT: RULE_DIRECT,
    TradeType.CONSIGNMENT: RULE_CONSIGNMENT,
    TradeType.SUBCONTRACT: RULE_SUBCONTRACT,
}
INTEREST_RULE = {
    TradeType.DIRECT: RULE_INTEREST_RETAIL,
    TradeType.CONSIGNMENT: RULE_INTEREST_RETAIL,
    TradeType.SUBCONTRACT: RULE_INTEREST_SUBCONTRACT,
}

# base-date field name per trade type, and documents that can establish it
BASE_FIELD = {
    TradeType.DIRECT: "goods_received_date",
    TradeType.CONSIGNMENT: "sales_close_date",
    TradeType.SUBCONTRACT: "goods_received_date",
}
BASE_LABEL = {
    TradeType.DIRECT: "상품수령일",
    TradeType.CONSIGNMENT: "월 판매마감일",
    TradeType.SUBCONTRACT: "목적물 수령일",
}
REQUIRED_DOCS = {
    "goods_received_date": (
        "상품 입고·하차 기록(상품수령일 확인용)",
        "검수확인서 또는 납품확인서",
    ),
    "sales_close_date": (
        "월 판매마감 내역(판매마감일 확인용)",
        "판매기간·마감일이 표시된 정산서",
    ),
    "object_received_date": ("목적물 수령증 또는 검수확인서(수령일 확인용)",),
    "trade_type": ("거래 형태(직매입·특약매입·하도급)를 확인할 수 있는 계약서",),
}

_DRF_NOTE = "법제처 DRF API로 본문 확인(2026-10-06)."


def default_registry() -> RuleRegistry:
    """The built-in rule versions. Unverified fields are flagged in ``source_note``."""
    return RuleRegistry(
        [
            RuleVersion(
                rule_id=RULE_DIRECT,
                version="2021-10-21",
                effective_from=date(2021, 10, 21),
                effective_to=None,
                known_from=date(2021, 4, 20),
                source_url=URL_ART8,
                params={"term_days": 60, "base": "goods_received_date"},
                title="대규모유통업법 제8조 제2항(직매입: 상품수령일부터 60일 이내)",
                source_note=_DRF_NOTE
                + " 조항 '<신설 2021.4.20>'; 시행일 2021-10-21은 SPEC §4 기준.",
                verified=True,
            ),
            RuleVersion(
                rule_id=RULE_DIRECT,
                version="2026-amendment",
                effective_from=None,  # INACTIVE until the enforcement date is fixed
                effective_to=None,
                known_from=date(2026, 9, 17),
                source_url=URL_ART8,
                params={
                    "term_days": 35,
                    "monthly_settlement_term_days": 20,
                    "monthly_settlement_base": "month_end",
                    "base": "goods_received_date",
                },
                title="개정안(직매입 35일, 월 1회 정산 시 매입마감 월말+20일) — 비활성",
                source_note="2026-09-17 국회 통과(SPEC §0). 시행일 미정이므로 effective_from=None.",
                verified=False,
            ),
            RuleVersion(
                rule_id=RULE_CONSIGNMENT,
                version="2012-01-01",
                effective_from=date(2012, 1, 1),
                effective_to=None,
                known_from=date(2011, 11, 14),
                source_url=URL_ART8,
                params={"term_days": 40, "base": "sales_close_date"},
                title="대규모유통업법 제8조 제1항(특약매입 등: 판매마감일부터 40일 이내)",
                source_note=_DRF_NOTE + " 효력 시작일(법 시행일)은 연혁 대조 전.",
                verified=False,
            ),
            RuleVersion(
                rule_id=RULE_CONSIGNMENT,
                version="2026-amendment",
                effective_from=None,
                effective_to=None,
                known_from=date(2026, 9, 17),
                source_url=URL_ART8,
                params={"term_days": 20, "base": "sales_close_date"},
                title="개정안(특약매입 등 20일) — 비활성",
                source_note="2026-09-17 국회 통과(SPEC §0). 시행일 미정이므로 effective_from=None.",
                verified=False,
            ),
            RuleVersion(
                rule_id=RULE_SUBCONTRACT,
                version="2009-04-01",
                effective_from=date(2009, 4, 1),
                effective_to=None,
                known_from=date(2009, 4, 1),
                source_url=URL_ART13,
                params={"term_days": 60, "base": "goods_received_date"},
                title="하도급법 제13조 제1항(목적물 등의 수령일부터 60일 이내)",
                source_note=_DRF_NOTE + " '[전문개정 2009.4.1]' 표기 기준; 시행일 연혁 대조 전.",
                verified=False,
            ),
            RuleVersion(
                rule_id=RULE_INTEREST_RETAIL,
                version="2021-13",
                effective_from=date(2021, 10, 21),
                effective_to=None,
                known_from=date(2021, 10, 21),
                source_url=URL_INTEREST_RETAIL,
                params={"annual_rate": "0.155", "day_count": 365},
                title=(
                    "상품판매대금 등 지연지급 시의 지연이율 고시(공정위 고시 제2021-13호): 연 15.5%"
                ),
                source_note=_DRF_NOTE + " 시행 2021-10-21. 이전 고시 버전은 등록하지 않음.",
                verified=True,
            ),
            RuleVersion(
                rule_id=RULE_INTEREST_SUBCONTRACT,
                version="2018-21",
                effective_from=date(2018, 12, 6),
                effective_to=None,
                known_from=date(2018, 12, 6),
                source_url=URL_INTEREST_SUBCONTRACT,
                params={"annual_rate": "0.155", "day_count": 365},
                title="선급금 등 지연지급 시의 지연이율 고시(공정위 고시 제2018-21호): 연 15.5%",
                source_note=_DRF_NOTE + " 시행 2018-12-06. 이전 고시 버전은 등록하지 않음.",
                verified=True,
            ),
        ]
    )


_DEFAULT_REGISTRY: RuleRegistry | None = None


def builtin_registry() -> RuleRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = default_registry()
    return _DEFAULT_REGISTRY


# ------------------------------------------------------------------ results
@dataclass(frozen=True)
class DueVariant:
    label: str  # "rollover_off" | "rollover_on"
    rollover: bool
    due_date: date
    delay_days: int
    interest: Money | None


@dataclass(frozen=True)
class DueResult:
    base_date: date
    due_date: date
    delay_days: int
    interest: Money | None
    assumptions: tuple[str, ...]
    variants: tuple[DueVariant, ...]
    rule_version: str
    interest_rule_version: str | None
    rounding: RoundingMode
    unresolved: tuple[str, ...]
    principal: Money
    end_date: date | None
    trade_type: TradeType
    term_days: int
    source_urls: tuple[str, ...]


@dataclass(frozen=True)
class Insufficient:
    missing: tuple[str, ...]
    required_documents: tuple[str, ...]
    notes: tuple[str, ...] = ()


def _pct(rate: str) -> str:
    return str((Decimal(rate) * 100).normalize())


def compute_interest(
    principal: Money, annual_rate: Decimal, delay_days: int, day_count: int, mode: RoundingMode
) -> Money:
    """principal x annual_rate x delay_days / day_count, rounded once with ``mode``."""
    if delay_days <= 0 or principal.amount <= 0:
        return Money.zero(principal.currency)
    value = Decimal(principal.amount) * annual_rate * Decimal(delay_days) / Decimal(day_count)
    return Money.from_decimal(value, mode, principal.currency)


def compute_due(
    trade_type: TradeType | str | None,
    base_date: date | None,
    principal: Money,
    *,
    paid_date: date | None = None,
    as_of: date | None = None,
    rollover: bool | None = None,
    rounding: RoundingMode | str = RoundingMode.FLOOR,
    calendar: KrCalendar | None = None,
    registry: RuleRegistry | None = None,
    known_at: date | None = None,
    monthly_settlement: bool | None = None,
    tax_invoice_date: date | None = None,
) -> DueResult | Insufficient:
    """Statutory due date, delay days and delay interest for one principal amount.

    ``paid_date`` (or, when unpaid, ``as_of``) ends the delay period.
    """
    rounding = RoundingMode(rounding)
    notes: list[str] = []
    if tax_invoice_date is not None:
        notes.append(f"세금계산서 작성일({tax_invoice_date.isoformat()})은 기산일로 사용하지 않음")
    if trade_type is None:
        return Insufficient(("trade_type",), REQUIRED_DOCS["trade_type"], tuple(notes))
    trade_type = TradeType(trade_type)
    field_name = BASE_FIELD[trade_type]
    if base_date is None:
        key = "object_received_date" if trade_type is TradeType.SUBCONTRACT else field_name
        notes.append(f"{BASE_LABEL[trade_type]} 미확인: 지급기한을 계산하지 않음")
        return Insufficient((key,), REQUIRED_DOCS[key], tuple(notes))

    reg = registry or builtin_registry()
    cal = calendar or default_calendar()
    rv: RuleVersion | None = reg.resolve(TERM_RULE[trade_type], base_date, known_at)
    if rv is None:
        notes.append(f"{base_date.isoformat()}에 적용되는 등록 규칙 버전 없음")
        return Insufficient(("rule_version",), (), tuple(notes))

    assumptions: list[str] = list(notes)
    unresolved: list[str] = []
    term = int(rv.params["term_days"])
    eff_base = base_date
    if "monthly_settlement_term_days" in rv.params:
        if monthly_settlement:
            term = int(rv.params["monthly_settlement_term_days"])
            eff_base = month_end(base_date)
            assumptions.append(f"월 1회 정산: 매입마감 월말({eff_base.isoformat()}) 기준")
        elif monthly_settlement is None:
            assumptions.append("월 1회 정산 여부 미확인: 일반 기한 적용")
    raw_due = add_days(eff_base, term)
    rolled = cal.next_business_day(raw_due)
    assumptions.append(
        f"{BASE_LABEL[trade_type]} {base_date.isoformat()} + {term}일 = {raw_due.isoformat()}"
        f" ({cal.describe(raw_due)}, 초일 불산입)"
    )

    if rollover is None:
        if rolled == raw_due:
            options = [("rollover_off", False, raw_due)]
            assumptions.append("기한 말일이 영업일이므로 rollover 조건과 무관")
        else:
            options = [("rollover_off", False, raw_due), ("rollover_on", True, rolled)]
            unresolved.append("rollover")
            assumptions.append(
                "기한 말일이 영업일이 아님: 다음 영업일 이월 여부(rollover) 미확인"
                " — 두 계산을 함께 표시"
            )
    elif rollover:
        options = [("rollover_on", True, rolled)]
        if rolled != raw_due:
            assumptions.append(f"rollover 적용: 다음 영업일 {rolled.isoformat()}")
    else:
        options = [("rollover_off", False, raw_due)]

    end = paid_date if paid_date is not None else as_of
    if end is None:
        assumptions.append(END_UNSET_NOTE)
    elif paid_date is None:
        assumptions.append(f"미지급: 기준일 {end.isoformat()}까지 지연일수 계산")

    irv = reg.resolve(INTEREST_RULE[trade_type], options[0][2] + timedelta(days=1), known_at)
    if irv is None:
        unresolved.append("interest_rule_version")
        assumptions.append("지연기간에 적용되는 이율 고시 버전이 등록되지 않음: 지연이자 미계산")
    else:
        assumptions.append(
            f"지연이자 = 원금 x 연 {_pct(irv.params['annual_rate'])}% x 지연일수 / "
            f"{irv.params['day_count']} ({rounding.value})"
        )

    variants: list[DueVariant] = []
    for label, roll, due in options:
        delay = max(0, days_between(due, end)) if end is not None else 0
        interest: Money | None = None
        if irv is not None:
            interest = compute_interest(
                principal,
                Decimal(irv.params["annual_rate"]),
                delay,
                int(irv.params["day_count"]),
                rounding,
            )
        variants.append(DueVariant(label, roll, due, delay, interest))

    primary = variants[0]
    urls = tuple(dict.fromkeys([rv.source_url] + ([irv.source_url] if irv else [])))
    return DueResult(
        base_date=base_date,
        due_date=primary.due_date,
        delay_days=primary.delay_days,
        interest=primary.interest,
        assumptions=tuple(assumptions),
        variants=tuple(variants),
        rule_version=rv.key,
        interest_rule_version=irv.key if irv else None,
        rounding=rounding,
        unresolved=tuple(unresolved),
        principal=principal,
        end_date=end,
        trade_type=trade_type,
        term_days=term,
        source_urls=urls,
    )
