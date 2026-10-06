"""Small hand-written fixtures for unit tests (not evaluation data)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from jettae.domain import (
    Agreement,
    BankTxn,
    Fact,
    Invoice,
    LineKind,
    Money,
    SettlementLine,
    SourceSpan,
    TradeType,
)
from jettae.evidence import AnalysisConfig, Snapshot

T = "t1"
NOW = datetime(2025, 11, 1, 0, 0, tzinfo=UTC)


def fact(fid: str, subject: str, value: object, doc: str = "docv-1", tenant: str = T) -> Fact:
    return Fact(
        id=fid,
        tenant_id=tenant,
        kind="amount",
        value=value,
        span=SourceSpan(doc, {"sheet": "Sheet1", "row": 2, "col": "D"}, str(value)),
        extractor="test",
        observed_at=NOW,
        subject_id=subject,
    )


def inv(
    iid: str,
    amount: int,
    cp: str = "가나유통",
    received: date | None = date(2025, 8, 7),
    trade: TradeType | None = TradeType.DIRECT,
    ref: str | None = None,
    issue: date | None = None,
    tenant: str = T,
) -> Invoice:
    return Invoice(
        id=iid,
        tenant_id=tenant,
        counterparty=cp,
        amount=Money(amount),
        issue_date=issue,
        reference=ref,
        trade_type=trade,
        goods_received_date=received,
        facts=(f"f-{iid}",),
    )


def line(
    lid: str,
    amount: int,
    ref: str | None = None,
    cp: str = "가나유통",
    received: date | None = date(2025, 8, 7),
    trade: TradeType | None = TradeType.DIRECT,
    kind: LineKind = LineKind.SALE,
    tenant: str = T,
) -> SettlementLine:
    return SettlementLine(
        id=lid,
        tenant_id=tenant,
        counterparty=cp,
        amount=Money(amount),
        line_kind=kind,
        goods_received_date=received,
        reference=ref,
        trade_type=trade,
        facts=(f"f-{lid}",),
    )


def txn(
    tid: str,
    amount: int,
    d: date,
    cp: str | None = "(주)가나유통",
    ref: str | None = None,
    memo: str = "",
    tenant: str = T,
) -> BankTxn:
    return BankTxn(
        id=tid,
        tenant_id=tenant,
        booked_date=d,
        amount=Money(amount),
        counterparty=cp,
        memo=memo,
        reference=ref,
        facts=(f"f-{tid}",),
    )


def agreement(aid: str, cp: str = "가나유통", rollover: bool | None = True, **kw) -> Agreement:
    return Agreement(
        id=aid, tenant_id=T, counterparty=cp, rollover=rollover, facts=(f"f-{aid}",), **kw
    )


def snapshot(
    invoices=(),
    txns=(),
    agreements=(),
    lines=(),
    as_of=date(2025, 11, 1),
    rollover=None,
    links=(),
) -> Snapshot:
    facts = []
    for r in [*invoices, *txns, *agreements, *lines]:
        for fid in r.facts:
            facts.append(fact(fid, r.id, getattr(getattr(r, "amount", None), "amount", r.id)))
    return Snapshot(
        tenant_id=T,
        invoices=tuple(invoices),
        settlement_lines=tuple(lines),
        bank_txns=tuple(txns),
        agreements=tuple(agreements),
        facts=tuple(facts),
        config=AnalysisConfig(as_of=as_of, rollover=rollover),
        evidence_links=tuple(links),
    )
