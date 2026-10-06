"""/public: unauthenticated, rate-limited calculators (no tenant data is read or stored).

``POST /public/due`` runs ``rules.kr_retail.compute_due`` (the same code as
``jettae rules due``) on the numbers the visitor types in. Nothing is persisted.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field, StrictBool, StrictInt

from jettae.api.errors import ERROR_RESPONSES
from jettae.api.schemas import _M
from jettae.db.plain import to_plain
from jettae.domain.money import Money, RoundingMode
from jettae.domain.status import TradeType
from jettae.rules.kr_retail import DueResult, compute_due

router = APIRouter(prefix="/public", tags=["public"], responses=ERROR_RESPONSES)

MAX_AMOUNT = 10**15  # won


def public_throttle(request: Request) -> None:
    limiter = getattr(request.app.state, "public_ip_limiter", None)
    if limiter is not None:
        limiter.hit(request.client.host if request.client else "unknown")


class PublicDueRequest(_M):
    trade_type: Literal["direct", "consignment", "subcontract"]
    base_date: date | None = Field(None, description="상품수령일·판매마감일 (YYYY-MM-DD)")
    paid_date: date | None = Field(None, description="지급일")
    as_of: date | None = Field(None, description="미지급이면 계산 기준일")
    amount: StrictInt = Field(..., ge=0, le=MAX_AMOUNT, description="원금(원, 정수)")
    rollover: StrictBool | None = Field(None, description="null = 두 계산 모두 표시")
    rounding: Literal["floor", "half_up"] = "floor"


@router.post("/due", dependencies=[Depends(public_throttle)])
def public_due(body: PublicDueRequest) -> Any:
    """법정 지급기한·지연일수·지연이자 검산(로그인 없음, 저장하지 않음).

    기준일이 없으면 계산하지 않고 필요한 서류를 돌려준다(result=insufficient)."""
    res = compute_due(
        TradeType(body.trade_type),
        body.base_date,
        Money(body.amount),
        paid_date=body.paid_date,
        as_of=body.as_of,
        rollover=body.rollover,
        rounding=RoundingMode(body.rounding),
    )
    kind = "due" if isinstance(res, DueResult) else "insufficient"
    return {"result": kind, **to_plain(res)}
