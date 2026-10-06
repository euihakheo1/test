"""Reconciliation inputs (receivable items, payments), normalisation and candidate filters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from jettae.domain.models import BankTxn, Invoice, SettlementLine
from jettae.domain.money import Money

_CORP_TOKENS = re.compile(
    r"\(주\)|㈜|주식회사|\(유\)|유한회사|\(재\)|co\.?,?\s*ltd\.?|inc\.?|corp\.?"
)
_NON_WORD = re.compile(r"[\s\-_.,·/()\[\]]+")
MIN_MEMO_REF_LEN = 4


def normalize_counterparty(name: str | None) -> str:
    """'(주)가나 유통', '가나유통 주식회사' -> '가나유통'."""
    if not name:
        return ""
    s = name.strip().lower()
    s = _CORP_TOKENS.sub("", s)
    return _NON_WORD.sub("", s)


def normalize_ref(ref: str | None) -> str:
    """Upper-case, strip whitespace and separators. Leading zeros are preserved."""
    if not ref:
        return ""
    return _NON_WORD.sub("", ref.strip().upper())


@dataclass(frozen=True)
class ReconConfig:
    days_before: int = 7  # payment may precede the item date by at most this many days
    days_after: int = 400  # ... and follow it by at most this many days
    fee_tolerance: Money = field(default_factory=Money.zero)  # payer-side fee shortfall
    max_candidates: int = 24
    max_subset_size: int = 6
    max_nodes: int = 200_000
    time_limit_s: float = 5.0  # safety cap; node cap is the deterministic bound


@dataclass(frozen=True)
class Item:
    """A receivable (invoice / settlement line). Negative amount = credit (공제·반품)."""

    id: str
    counterparty: str
    amount: Money
    date: date | None
    reference: str = ""
    group_ref: str = ""  # e.g. settlement statement id shared by several lines
    # references of corroborating documents of the same receivable (e.g. the approval number
    # of a tax invoice linked to a settlement line): a payment quoting them pays this item
    alt_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Payment:
    id: str
    counterparty: str
    amount: Money
    date: date
    reference: str = ""
    memo: str = ""


def item_from_invoice(inv: Invoice) -> Item:
    d = inv.issue_date or inv.goods_received_date or inv.sales_close_date
    return Item(
        inv.id,
        normalize_counterparty(inv.counterparty),
        inv.amount,
        d,
        normalize_ref(inv.reference),
    )


def item_from_line(line: SettlementLine) -> Item:
    d = line.sales_close_date or line.period_end or line.goods_received_date
    return Item(
        line.id,
        normalize_counterparty(line.counterparty),
        line.amount,
        d,
        normalize_ref(line.reference),
        normalize_ref(line.settlement_ref),
    )


def payment_from_txn(txn: BankTxn, counterparty: str | None = None) -> Payment:
    return Payment(
        txn.id,
        counterparty if counterparty is not None else normalize_counterparty(txn.counterparty),
        txn.amount,
        txn.booked_date,
        normalize_ref(txn.reference),
        normalize_ref(txn.memo),
    )


def in_window(item: Item, payment: Payment, cfg: ReconConfig) -> bool:
    if item.date is None:
        return True
    delta = (payment.date - item.date).days
    return -cfg.days_before <= delta <= cfg.days_after


def reference_hits(payment: Payment, items: list[Item]) -> list[Item]:
    """Items whose reference (or settlement group ref) equals the payment reference or
    appears in the payment memo (refs shorter than MIN_MEMO_REF_LEN only match exactly)."""
    hits: list[Item] = []
    for it in items:
        for ref in (it.reference, it.group_ref, *it.alt_refs):
            if not ref:
                continue
            if ref == payment.reference or (
                len(ref) >= MIN_MEMO_REF_LEN and payment.memo and ref in payment.memo
            ):
                hits.append(it)
                break
    return hits
