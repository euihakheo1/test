"""홈택스 전자세금계산서 목록 (엑셀 내려받기) → :class:`Invoice` records.

Sources checked 2026-10-06:
- 손택스 "전자(세금)계산서 목록" screen (official, NTS):
  https://mob.tbet.hometax.go.kr/jsonAction.do?actionId=UTBETGBA01F001
  confirms the vocabulary 작성일자 / 발급일자 / 전송일자, 종류(1) 세금계산서·수정세금계산서,
  종류(2) 일반·영세율·위수탁·수입·위수탁영세율·수입납부유예, 발급유형 인터넷발급·ARS발급·VAN발급·
  ASP발급·자체발급·겸용서식발급·대리발급·관세청발급·모바일발급, and list columns 거래처상호, 성명,
  공급가액, 세액, 작성일자, 발급일자, 전송일자, 품목명, 비고, 승인번호.
- Download procedure (목록조회 → 내려받기, "품목 정보 추가" option, 1~1000건 per file):
  https://www.entax.co.kr/cs/help/content.php?k=lblhometaxdownload1 and
  http://www.os21.net/newjj/help/Guide.html
- 승인번호 is 24 characters (8-digit 작성일 + 8 + 8): NTS 상담 FAQ search snippet
  (https://call.nts.go.kr/call/qna/selectHomeQnaInfo.do?mi=12941&ctgId=CTG11715), not fetched.

NOT verified: the exact column order/labels of the PC-홈택스 Excel file (e.g. 공급자사업자등록번호,
종사업장번호, 상호, 대표자명 repeated for 공급받는자, 합계금액, 전자세금계산서분류,
전자세금계산서종류,
발급유형, 영수/청구 구분, 품목일자 …). No public page listing them was found, so recognition is
synonym-based (order-independent) and repeated party columns (상호, 대표자명, 종사업장번호 …) are
disambiguated by the nearest preceding 공급자/공급받는자 column. Unknown layouts go through the
column-mapping confirmation step.

The 작성일자 is kept as ``issue_date`` only. It is never used as 상품수령일/판매마감일.
"""

from __future__ import annotations

from collections.abc import Sequence

from jettae.domain.models import Invoice
from jettae.domain.money import Money
from jettae.domain.status import DocKind
from jettae.ingest.formats.base import FormatSpec, RowReader, missing_base_fields
from jettae.ingest.mapping import FieldKind, FieldSpec, normalize_header

F = FieldKind
_SUP = ("공급자",)
FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("issue_date", "작성일자", F.DATE, ("작성일자", "작성일"), True, ("품목",)),
    FieldSpec("approval_no", "승인번호", F.ID, ("승인번호", "국세청승인번호"), True),
    FieldSpec("issued_date", "발급일자", F.DATE, ("발급일자", "발급일")),
    FieldSpec("sent_date", "전송일자", F.DATE, ("전송일자", "국세청전송일자")),
    FieldSpec(
        "supplier_brn",
        "공급자 사업자등록번호",
        F.BRN,
        ("공급자사업자등록번호", "공급자등록번호"),
        exclude=("종사업장",),
    ),
    FieldSpec("supplier_sub", "공급자 종사업장번호", F.ID, ("공급자종사업장번호",)),
    FieldSpec("supplier_name", "공급자 상호", F.TEXT, ("공급자상호", "공급자상호법인명")),
    FieldSpec("supplier_ceo", "공급자 대표자명", F.TEXT, ("공급자대표자명", "공급자성명")),
    FieldSpec(
        "buyer_brn",
        "공급받는자 사업자등록번호",
        F.BRN,
        ("공급받는자사업자등록번호", "공급받는자등록번호"),
        exclude=("종사업장",),
    ),
    FieldSpec("buyer_sub", "공급받는자 종사업장번호", F.ID, ("공급받는자종사업장번호",)),
    FieldSpec("buyer_name", "공급받는자 상호", F.TEXT, ("공급받는자상호", "공급받는자상호법인명")),
    FieldSpec("buyer_ceo", "공급받는자 대표자명", F.TEXT, ("공급받는자대표자명", "공급받는자성명")),
    FieldSpec("party_name", "거래처상호", F.TEXT, ("거래처상호", "거래처명")),
    FieldSpec("total", "합계금액", F.AMOUNT, ("합계금액", "합계"), True, ("품목",)),
    FieldSpec("supply", "공급가액", F.AMOUNT, ("공급가액",), exclude=("품목",)),
    FieldSpec("tax", "세액", F.AMOUNT, ("세액",), exclude=("품목",)),
    FieldSpec("invoice_class", "전자세금계산서분류", F.TEXT, ("전자세금계산서분류", "분류")),
    FieldSpec("invoice_type", "전자세금계산서종류", F.TEXT, ("전자세금계산서종류", "종류")),
    FieldSpec("issue_type", "발급유형", F.TEXT, ("발급유형",)),
    FieldSpec("remark", "비고", F.TEXT, ("비고",), exclude=("품목",)),
    FieldSpec("receipt_claim", "영수/청구 구분", F.TEXT, ("영수청구구분", "영수청구")),
    FieldSpec("item_date", "품목일자", F.DATE, ("품목일자",)),
    FieldSpec("item_name", "품목명", F.TEXT, ("품목명",)),
    FieldSpec("item_supply", "품목공급가액", F.AMOUNT, ("품목공급가액",)),
    FieldSpec("item_tax", "품목세액", F.AMOUNT, ("품목세액",)),
)

_AMBIGUOUS = {
    "상호",
    "상호법인명",
    "대표자명",
    "성명",
    "주소",
    "종사업장번호",
    "종사업장",
    "사업자등록번호",
    "등록번호",
    "이메일",
    "업태",
    "종목",
}


def contextualize(headers: Sequence[str]) -> list[str]:
    """Prefix repeated party columns with the nearest preceding 공급자/공급받는자 column."""
    party: str | None = None
    out: list[str] = []
    for h in headers:
        n = normalize_header(h)
        if n.startswith("공급받는자"):
            party = "공급받는자"
        elif n.startswith("공급자"):
            party = "공급자"
        if party and n in _AMBIGUOUS:
            out.append(f"{party} {h.strip()}")
        else:
            out.append(h)
    return out


def _direction(r: RowReader, supplier_brn: str | None, buyer_brn: str | None) -> str | None:
    opts = r.ctx.options
    if opts.self_brn:
        mine = "".join(ch for ch in opts.self_brn if ch.isdigit())

        def digits(x: str | None) -> str:
            return "".join(ch for ch in (x or "") if ch.isdigit())

        if mine and digits(supplier_brn) == mine:
            return "sales"
        if mine and digits(buyer_brn) == mine:
            return "purchase"
    if opts.direction in ("sales", "purchase"):
        return opts.direction
    hint = r.ctx.doc_hint
    has_s, has_p = "매출" in hint, "매입" in hint
    if has_s != has_p:
        return "sales" if has_s else "purchase"
    return None


def build(r: RowReader) -> Invoice | None:
    issue_date = r.date("issue_date")
    approval = r.ident("approval_no")
    supplier_brn = r.brn("supplier_brn")
    buyer_brn = r.brn("buyer_brn")
    direction = _direction(r, supplier_brn, buyer_brn)
    if direction == "purchase":
        r._issue(None, "매입 세금계산서: 받을 돈(매출채권)이 아니므로 레코드를 만들지 않음")
        return None
    total = r.amount("total")
    supply = r.amount("supply")
    tax = r.amount("tax")
    if total is None:
        if supply is None:
            r._issue("total", "합계금액·공급가액이 모두 없음")
            return None
        total = supply + (tax or 0)
        r.add_fact("total_derived", Money(total), None, note="공급가액+세액")
    elif supply is not None and tax is not None and supply + tax != total:
        r._issue("total", f"합계금액 {total} ≠ 공급가액 {supply} + 세액 {tax}")
    missing = missing_base_fields(None, None, None)
    counterparty = ""
    if direction == "sales":
        counterparty = r.text("buyer_name") or r.text("party_name") or ""
    else:
        # Unknown direction: this may be a purchase invoice (money the user owes), so the
        # record carries no counterparty and the evidence layer never counts it as a
        # receivable. The value issue makes the version ``applied_needs_ack`` so the user
        # is told to give the direction (mapping options self_brn / direction).
        r.add_fact("direction", "unknown", None)
        missing.add("direction")
        r._issue(
            "direction",
            "매출·매입 구분을 정할 수 없음(우리 회사 사업자등록번호 또는 매출/매입 선택 필요): "
            "받을 돈으로 계산하지 않음",
        )
    if not counterparty:
        missing.add("counterparty")
        if direction == "sales":
            r._issue("buyer_name", "공급받는자(거래처) 상호가 없음: 받을 돈으로 계산하지 않음")
    if issue_date is None:
        missing.add("issue_date")
    if not approval:
        missing.add("reference")
    r.read_all_other(set())
    return Invoice(
        id=r.subject_id,
        tenant_id=r.ctx.tenant_id,
        counterparty=counterparty,
        amount=Money(total, r.ctx.options.currency),
        issue_date=issue_date,
        reference=approval,
        trade_type=None,
        goods_received_date=None,
        sales_close_date=None,
        facts=tuple(f.id for f in r.facts),
        missing=frozenset(missing),
    )


HOMETAX = FormatSpec(
    id="hometax_etax_list",
    title="홈택스 전자세금계산서 목록",
    doc_kind=DocKind.TAX_INVOICE,
    entity="invoice",
    id_prefix="inv",
    fields=FIELDS,
    markers=(
        "승인번호",
        "작성일자",
        "공급가액",
        "세액",
        "합계금액",
        "전자세금계산서분류",
        "발급유형",
    ),
    build=build,
    key_fields=("approval_no", "issue_date", "total"),
    contextualize=contextualize,
    sources=(
        "https://mob.tbet.hometax.go.kr/jsonAction.do?actionId=UTBETGBA01F001",
        "https://www.entax.co.kr/cs/help/content.php?k=lblhometaxdownload1",
        "http://www.os21.net/newjj/help/Guide.html",
    ),
    verification=(
        "vocabulary and list columns from the official 손택스 list screen; exact PC Excel "
        "column order not verified (no public listing found)"
    ),
    total_fields=("total", "supply", "tax"),
)
