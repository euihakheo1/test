"""Generic retailer settlement sheets (정산서) and agreement-term sheets via column mapping.

Retailer layouts differ per company and are not public, so these recognizers rely on header
synonyms plus the confirmation step. Base dates are read only from their own columns:
``goods_received_date`` from 상품수령일/입고일/하차일, ``sales_close_date`` from 판매마감일.
지급일·정산일·세금계산서 작성일 columns are kept as facts and never used as base dates.
``납품일`` is deliberately *not* a synonym of 상품수령일: the user must map it explicitly.
"""

from __future__ import annotations

from jettae.domain.models import Agreement, SettlementLine
from jettae.domain.money import Money
from jettae.domain.status import DocKind, LineKind
from jettae.ingest.formats.base import (
    FormatSpec,
    RowReader,
    missing_base_fields,
    trade_type_from_text,
)
from jettae.ingest.mapping import FieldKind, FieldSpec, normalize_header

F = FieldKind
NOT_PLANNED = ("예정", "계획")

SETTLE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "counterparty",
        "거래처(유통사)",
        F.TEXT,
        ("거래처", "거래처명", "유통사", "유통업체", "판매처", "업체명"),
    ),
    FieldSpec(
        "settlement_ref",
        "정산번호",
        F.ID,
        ("정산번호", "정산서번호", "정산id", "정산서id", "지급번호", "전표번호"),
    ),
    FieldSpec(
        "reference",
        "발주/거래번호",
        F.ID,
        ("발주번호", "주문번호", "납품번호", "거래번호", "입고번호", "po번호"),
    ),
    FieldSpec(
        "amount",
        "정산금액",
        F.AMOUNT,
        ("정산금액", "지급금액", "지급액", "정산액", "지급대상금액", "금액"),
        True,
        ("공제", "수수료", "세액", "부가세"),
    ),
    FieldSpec("deduction", "공제금액", F.AMOUNT, ("공제금액", "공제액", "차감액", "공제")),
    FieldSpec("fee", "수수료", F.AMOUNT, ("수수료", "판매수수료", "수수료금액")),
    FieldSpec(
        "line_kind", "구분", F.TEXT, ("구분", "항목", "정산구분", "유형", "내역구분", "정산항목")
    ),
    FieldSpec(
        "trade_type",
        "거래형태",
        F.TEXT,
        ("거래형태", "거래유형", "매입형태", "계약형태", "매입유형"),
    ),
    FieldSpec(
        "period_start",
        "정산기간 시작",
        F.DATE,
        ("정산시작일", "기간시작일", "판매시작일", "정산기간시작"),
    ),
    FieldSpec(
        "period_end",
        "정산기간 종료",
        F.DATE,
        ("정산종료일", "기간종료일", "판매종료일", "정산기간종료"),
    ),
    FieldSpec(
        "sales_close_date",
        "판매마감일",
        F.DATE,
        ("판매마감일", "월판매마감일", "판매마감일자", "마감일", "마감일자"),
        exclude=NOT_PLANNED,
    ),
    FieldSpec(
        "goods_received_date",
        "상품수령일",
        F.DATE,
        (
            "상품수령일",
            "수령일",
            "수령일자",
            "입고일",
            "입고일자",
            "입고확정일",
            "상품하차일",
            "하차일",
            "하차일자",
        ),
        exclude=NOT_PLANNED,
    ),
    FieldSpec(
        "payment_date",
        "지급일",
        F.DATE,
        ("지급일", "지급예정일", "정산일", "지급일자", "입금예정일"),
    ),
    FieldSpec(
        "tax_invoice_date",
        "세금계산서 작성일",
        F.DATE,
        ("세금계산서일자", "계산서일자", "작성일자"),
    ),
    FieldSpec("item", "상품명", F.TEXT, ("상품명", "품목명", "품명")),
)

_KIND_WORDS = (
    (("공제", "차감", "판촉비", "장려금", "광고비"), LineKind.DEDUCTION),
    (("반품",), LineKind.RETURN),
    (("수수료",), LineKind.FEE),
    (("환불",), LineKind.REFUND),
    (("매출", "판매", "매입", "납품", "정상", "입고"), LineKind.SALE),
)


def line_kind_from_text(s: str | None) -> LineKind | None:
    if not s:
        return None
    n = normalize_header(s)
    for words, kind in _KIND_WORDS:
        if any(w in n for w in words):
            return kind
    return None


def build_settlement(r: RowReader) -> SettlementLine | None:
    amount = r.amount("amount")
    if amount is None:
        if not r.has("amount"):
            r._issue("amount", "정산금액 없음")
        return None
    kind_text = r.text("line_kind")
    kind = line_kind_from_text(kind_text)
    if kind is None:
        kind = LineKind.DEDUCTION if amount < 0 else LineKind.SALE
        r.add_fact(
            "line_kind_inferred",
            kind.value,
            r.cell("line_kind"),
            note="구분 열이 없거나 인식되지 않아 금액 부호로 정함",
        )
    tt_text = r.text("trade_type")
    trade_type = trade_type_from_text(tt_text)
    if tt_text and trade_type is None:
        r._issue("trade_type", f"거래형태를 인식하지 못함: {tt_text!r}")
    received = r.date("goods_received_date")
    close = r.date("sales_close_date")
    # Counterparty: the user's explicit document-level override wins; otherwise the mapped
    # column of this row. The column value is always cell *text* (never a column index).
    override = r.ctx.options.counterparty_override
    cp_col = r.text("counterparty")
    counterparty: str | None
    if isinstance(override, str) and override.strip():
        counterparty = override.strip()
        r.add_fact("counterparty_option", counterparty, None, note="업로드 시 사용자가 지정")
    else:
        counterparty = cp_col
    missing = missing_base_fields(trade_type, received, close)
    if not counterparty:
        missing.add("counterparty")
    period_start = r.date("period_start")
    period_end = r.date("period_end")
    sref = r.ident("settlement_ref")
    ref = r.ident("reference")
    r.read_all_other(set())
    return SettlementLine(
        id=r.subject_id,
        tenant_id=r.ctx.tenant_id,
        counterparty=counterparty or "",
        amount=Money(amount, r.ctx.options.currency),
        line_kind=kind,
        period_start=period_start,
        period_end=period_end,
        sales_close_date=close,
        goods_received_date=received,
        settlement_ref=sref,
        reference=ref,
        trade_type=trade_type,
        facts=tuple(f.id for f in r.facts),
        missing=frozenset(missing),
    )


SETTLEMENT = FormatSpec(
    id="retail_settlement",
    title="유통사 정산서 (열 매핑)",
    doc_kind=DocKind.SETTLEMENT,
    entity="settlement_line",
    id_prefix="stl",
    fields=SETTLE_FIELDS,
    markers=("정산금액", "판매마감일", "상품수령일", "거래형태", "정산번호", "공제금액"),
    build=build_settlement,
    key_fields=(
        "settlement_ref",
        "reference",
        "amount",
        "goods_received_date",
        "sales_close_date",
        "line_kind",
    ),
    verification="generic column mapping; retailer layouts are not public",
    total_fields=("amount",),
)


AGREE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "counterparty",
        "거래처",
        F.TEXT,
        ("거래처", "거래처명", "유통사", "계약상대방", "상대방"),
        True,
    ),
    FieldSpec(
        "trade_type", "거래형태", F.TEXT, ("거래형태", "거래유형", "매입형태", "계약형태"), True
    ),
    FieldSpec(
        "payment_term_days",
        "대금지급기한(일)",
        F.INT,
        ("지급기한일", "대금지급기한", "지급기일", "지급기한", "대금지급기일", "지급조건일수"),
    ),
    FieldSpec(
        "valid_from", "계약 시작일", F.DATE, ("계약시작일", "계약기간시작", "적용시작일", "계약일")
    ),
    FieldSpec(
        "valid_to", "계약 종료일", F.DATE, ("계약종료일", "계약기간종료", "적용종료일", "만료일")
    ),
)


def build_agreement(r: RowReader) -> Agreement | None:
    cp = r.text("counterparty")
    if not cp:
        r._issue("counterparty", "거래처 없음")
        return None
    tt_text = r.text("trade_type")
    tt = trade_type_from_text(tt_text)
    if tt_text and tt is None:
        r._issue("trade_type", f"거래형태를 인식하지 못함: {tt_text!r}")
    days = r.integer("payment_term_days")
    vf = r.date("valid_from")
    vt = r.date("valid_to")
    missing = {
        name
        for name, v in (("trade_type", tt), ("payment_term_days", days), ("valid_from", vf))
        if v is None
    }
    # rollover / rounding / monthly settlement are never inferred from a term sheet
    missing |= {"rollover", "rounding"}
    return Agreement(
        id=r.subject_id,
        tenant_id=r.ctx.tenant_id,
        counterparty=cp,
        trade_type=tt,
        valid_from=vf,
        valid_to=vt,
        payment_term_days=days,
        facts=tuple(f.id for f in r.facts),
        missing=frozenset(missing),
    )


AGREEMENT = FormatSpec(
    id="agreement_terms",
    title="거래 약정 조건표 (열 매핑)",
    doc_kind=DocKind.AGREEMENT,
    entity="agreement",
    id_prefix="agr",
    fields=AGREE_FIELDS,
    markers=("지급기한", "계약시작일", "계약종료일", "거래형태", "대금지급기일"),
    build=build_agreement,
    key_fields=("counterparty", "trade_type", "valid_from"),
    verification="generic column mapping",
)
