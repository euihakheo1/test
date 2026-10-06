"""Korean bank transaction exports (거래내역 조회 → 엑셀/CSV 저장) → :class:`BankTxn`.

One synonym-based recognizer covers KB국민, 신한, 우리, 하나, NH농협, IBK기업 layouts. Header
vocabulary sources (checked 2026-10-06; all *secondary*, no bank publishes its export schema):
- KB국민 "거래일시 / 적요 / 기재내용 / 찾으신금액 / 맡기신금액 / 잔액 / 거래점" and 신한
  "거래일자 / 시간 / 적요 / 출금액 / 입금액 / 잔액 / 거래기점" (statement layouts),
  우리 date+time in
  one column, 하나 2025 change merging 거래종류 into 적요:
  https://www.lido.app/kr/eunhaeng-georaenaeyeok
- Six-bank converter notes (신한 거래점 separate column, 우리 date+time combined):
  https://www.semu.ai.kr/tools/bank-convert
- 신한 statement fields incl. 입금자명/거래점: https://mooders.co.kr/shinhan-transaction-statement/

NOT verified against real exports: exact labels for 우리/하나/NH농협/IBK (e.g. 의뢰인/수취인,
거래후잔액, 취급점), and whether a given bank's ".xls" is binary or HTML. ``bank_hint`` is only
a label from the preamble/filename; it does not change parsing. Unknown layouts go through the
column-mapping confirmation step.

Sign convention: positive = money received (입금), negative = paid out (출금).
"""

from __future__ import annotations

import re

from jettae.domain.models import BankTxn
from jettae.domain.money import Money
from jettae.domain.status import DocKind
from jettae.ingest.formats.base import FormatSpec, RowReader
from jettae.ingest.mapping import FieldKind, FieldSpec, normalize_header

F = FieldKind
FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "txn_date",
        "거래일시/거래일자",
        F.DATE,
        (
            "거래일시",
            "거래일자",
            "거래일",
            "거래날짜",
            "입출금일자",
            "입출금일시",
            "처리일시",
            "일자",
            "날짜",
        ),
        True,
        ("기산", "예정", "만기"),
    ),
    FieldSpec(
        "txn_time", "거래시간", F.TIME, ("거래시간", "거래시각", "시간", "시각"), exclude=("일시",)
    ),
    FieldSpec("memo", "적요", F.TEXT, ("적요", "거래구분", "거래종류", "구분")),
    FieldSpec(
        "description",
        "기재내용/내용",
        F.TEXT,
        (
            "기재내용",
            "거래내용",
            "내용",
            "거래기록사항",
            "통장표시내용",
            "송금메모",
            "메모",
            "비고",
        ),
    ),
    FieldSpec(
        "counterparty",
        "보낸분/받는분",
        F.TEXT,
        (
            "보낸분받는분",
            "받는분보낸분",
            "의뢰인수취인",
            "의뢰인",
            "입금자명",
            "입금자",
            "보낸분",
            "받는분",
            "거래상대",
            "상대방",
            "예금주명",
        ),
    ),
    FieldSpec(
        "deposit",
        "입금액/맡기신금액",
        F.AMOUNT,
        ("입금액", "맡기신금액", "입금", "입금금액", "들어온금액"),
        exclude=("입금자", "입금은행", "입금계좌", "입금일"),
    ),
    FieldSpec(
        "withdrawal",
        "출금액/찾으신금액",
        F.AMOUNT,
        ("출금액", "찾으신금액", "출금", "출금금액", "지급액", "나간금액"),
        exclude=("출금계좌", "출금일", "출금은행"),
    ),
    FieldSpec(
        "amount", "거래금액", F.AMOUNT, ("거래금액", "금액"), exclude=("잔액", "입금", "출금")
    ),
    FieldSpec("balance", "잔액", F.AMOUNT, ("잔액", "거래후잔액", "잔고", "거래후잔고")),
    FieldSpec(
        "branch",
        "거래점",
        F.TEXT,
        ("거래점", "거래점명", "거래기점", "취급점", "처리점", "거래지점"),
    ),
)

BANK_NAMES = {
    "kb": ("국민은행", "kb국민", "kbstar", "국민"),
    "shinhan": ("신한은행", "신한"),
    "woori": ("우리은행", "우리"),
    "hana": ("하나은행", "하나"),
    "nh": ("농협은행", "nh농협", "농협"),
    "ibk": ("기업은행", "ibk"),
}
_ACCOUNT = re.compile(r"(?:계좌번호|계좌|출금계좌)\s*[:：]?\s*([0-9][0-9\-]{7,})")


def bank_hint(text: str) -> str | None:
    t = normalize_header(text)
    for code, names in BANK_NAMES.items():
        if any(normalize_header(n) in t for n in names[:2]):
            return code
    for code, names in BANK_NAMES.items():
        if any(normalize_header(n) in t for n in names[2:]):
            return code
    return None


def account_from_preamble(text: str) -> str | None:
    m = _ACCOUNT.search(text)
    return m.group(1) if m else None


def build(r: RowReader) -> BankTxn | None:
    booked = r.date("txn_date")
    if booked is None:
        if r.has("txn_date"):
            return None  # unparseable date: issue already recorded
        r._issue("txn_date", "거래일자 없음")
        return None
    if not any(f.kind.endswith("txn_date_time") for f in r.facts):
        r.time("txn_time")
    dep = r.amount("deposit")
    wd = r.amount("withdrawal")
    amount: int | None = None
    if dep or wd:
        if dep and wd:
            r._issue(None, f"입금 {dep}과 출금 {wd}이 한 행에 함께 있음: 레코드를 만들지 않음")
            return None
        amount = dep if dep else -(wd or 0)
    else:
        signed = r.amount("amount")
        if signed is not None and signed != 0:
            kind_text = (r.raw_text("memo") + " " + r.raw_text("description")).strip()
            if signed < 0 or "입금" in kind_text and "출금" not in kind_text:
                amount = signed
            elif "출금" in kind_text and "입금" not in kind_text:
                amount = -signed
            else:
                r._issue("amount", "거래금액의 입금/출금 방향을 알 수 없음")
                return None
    if amount is None or amount == 0:
        if dep == 0 or wd == 0 or r.has("amount"):
            r._issue(None, "금액 0: 레코드를 만들지 않음")
        else:
            r._issue(None, "입금액/출금액 없음")
        return None
    memo_t = r.text("memo")
    desc_t = r.text("description")
    cp = r.text("counterparty")
    r.amount("balance")
    r.text("branch")
    memo = " ".join(x for x in (memo_t, desc_t, cp) if x)
    if any(k in memo for k in ("취소", "정정", "반환", "오입금")):
        r.add_fact("reversal_marker", True, r.cell("memo") or r.cell("description"))
    missing = set() if cp else {"counterparty"}
    account = r.ctx.options.account_override or account_from_preamble(r.ctx.doc_hint)
    return BankTxn(
        id=r.subject_id,
        tenant_id=r.ctx.tenant_id,
        booked_date=booked,
        amount=Money(amount, r.ctx.options.currency),
        counterparty=cp,
        memo=memo,
        reference=None,
        account=account,
        facts=tuple(f.id for f in r.facts),
        missing=frozenset(missing),
    )


BANK = FormatSpec(
    id="kr_bank_txn",
    title="국내 은행 거래내역",
    doc_kind=DocKind.BANK,
    entity="bank_txn",
    id_prefix="bank",
    fields=FIELDS,
    markers=("맡기신금액", "찾으신금액", "입금액", "출금액", "잔액", "적요", "거래점", "기재내용"),
    build=build,
    key_fields=(
        "txn_date",
        "txn_time",
        "deposit",
        "withdrawal",
        "amount",
        "balance",
        "memo",
        "description",
    ),
    required_any=(("deposit", "withdrawal", "amount"),),
    sources=(
        "https://www.lido.app/kr/eunhaeng-georaenaeyeok",
        "https://www.semu.ai.kr/tools/bank-convert",
        "https://mooders.co.kr/shinhan-transaction-statement/",
    ),
    verification="secondary sources only (KB, 신한 headers); others unverified",
    total_fields=("deposit", "withdrawal", "amount"),
)
