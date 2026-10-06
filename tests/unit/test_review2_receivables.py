"""Second external review (2026-10-06), pure evidence rules (hand-written fixtures):

* rule 6: an invoice whose direction / counterparty is unknown is never counted
* rule 5: the same settlement line in two document versions is counted once until confirmed;
  two lines of ONE document are never duplicates; incremental == full recompute
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from jettae.domain.models import (
    BankTxn,
    Change,
    EvidenceLink,
    Fact,
    Invoice,
    LinkRelation,
    PendingKind,
    SettlementLine,
    SourceSpan,
)
from jettae.domain.money import Money
from jettae.domain.status import ChangeKind, ReconcileStatus, TradeType
from jettae.evidence.engine import full_recompute, incremental_recompute
from jettae.evidence.snapshot import AnalysisConfig, Snapshot

T = "t"
CFG = AnalysisConfig(as_of=date(2025, 11, 1), rollover=False)


def _fact(fid: str, subject: str, dvid: str, at: datetime) -> Fact:
    return Fact(
        id=fid,
        tenant_id=T,
        kind="settlement_line.amount",
        value=1,
        span=SourceSpan(dvid, {"row": 2}, "1000"),
        extractor="test",
        observed_at=at,
        subject_id=subject,
    )


def _line(lid: str, ref: str, amount: int = 1000) -> SettlementLine:
    return SettlementLine(
        id=lid,
        tenant_id=T,
        counterparty="가나유통",
        amount=Money(amount),
        trade_type=TradeType.DIRECT,
        goods_received_date=date(2025, 8, 7),
        reference=ref,
        facts=(f"f:{lid}",),
    )


T1 = datetime(2025, 9, 1, tzinfo=UTC)
T2 = datetime(2025, 9, 2, tzinfo=UTC)


def _snap(lines: list[SettlementLine], origins: dict[str, tuple[str, datetime]], **kw) -> Snapshot:
    facts = tuple(_fact(f"f:{lid}", lid, dv, at) for lid, (dv, at) in origins.items())
    return Snapshot(tenant_id=T, settlement_lines=tuple(lines), facts=facts, config=CFG, **kw)


def test_invoice_without_direction_is_pending_and_never_counted():
    inv = Invoice(
        id="inv:1",
        tenant_id=T,
        counterparty="",
        amount=Money(1100),
        reference="20250807-1",
        missing=frozenset({"direction", "counterparty", "trade_type"}),
    )
    line = _line("stl:1", "PO-1", 1100)
    snap = Snapshot(tenant_id=T, invoices=(inv,), settlement_lines=(line,), config=CFG)
    rec = snap.economic_receivables["inv:1"]
    assert rec.counted is False and rec.pending is PendingKind.INVOICE_DIRECTION
    assert rec.missing == ("counterparty", "direction")
    assert snap.counted_total() == 1100
    res = full_recompute(snap)
    d = res.decisions["dec:inv:1"]
    assert d.status is ReconcileStatus.INSUFFICIENT_EVIDENCE
    assert "invoice_direction" in d.unresolved and {"direction", "counterparty"} <= set(d.missing)
    # a separate_sale confirmation does not override an unknown direction
    link = EvidenceLink("elink:inv:1", T, "inv:1", LinkRelation.SEPARATE_SALE)
    snap2 = Snapshot(
        tenant_id=T, invoices=(inv,), settlement_lines=(line,), config=CFG, evidence_links=(link,)
    )
    assert snap2.counted_total() == 1100


def test_line_repeated_in_another_document_is_counted_once():
    a, b = _line("stl:a", "PO-1"), _line("stl:b", "po 1")  # same normalised reference
    snap = _snap([a, b], {"stl:a": ("dv-1", T1), "stl:b": ("dv-2", T2)})
    recs = snap.economic_receivables
    assert recs["stl:a"].counted is True
    assert recs["stl:b"].counted is False and recs["stl:b"].pending is PendingKind.DUPLICATE_LINE
    assert [c.id for c in recs["stl:b"].candidates] == ["stl:a"]
    assert snap.counted_total() == 1000
    # the earlier document (by registration time) is the basis, whatever the ids are
    flipped = _snap([a, b], {"stl:a": ("dv-1", T2), "stl:b": ("dv-2", T1)})
    assert flipped.economic_receivables["stl:b"].counted is True
    assert flipped.economic_receivables["stl:a"].counted is False


def test_two_lines_of_one_document_are_not_duplicates():
    a, b = _line("stl:a", "PO-1"), _line("stl:b", "PO-1")
    snap = _snap([a, b], {"stl:a": ("dv-1", T1), "stl:b": ("dv-1", T1)})
    assert all(r.counted for r in snap.economic_receivables.values())
    assert snap.counted_total() == 2000
    # rows without provenance (hand-entered) are not compared either
    bare = Snapshot(tenant_id=T, settlement_lines=(a, b), config=CFG)
    assert bare.counted_total() == 2000


def test_duplicate_confirmations():
    a, b = _line("stl:a", "PO-1"), _line("stl:b", "PO-1")
    origins = {"stl:a": ("dv-1", T1), "stl:b": ("dv-2", T2)}
    sep = EvidenceLink(
        "elink:stl:b", T, "stl:b", LinkRelation.SEPARATE_SALE, document_entity="settlement_line"
    )
    snap = _snap([a, b], origins, evidence_links=(sep,))
    assert snap.counted_total() == 2000
    same = EvidenceLink(
        "elink:stl:b",
        T,
        "stl:b",
        LinkRelation.SAME_SALE,
        settlement_line_id="stl:a",
        document_entity="settlement_line",
    )
    snap = _snap([a, b], origins, evidence_links=(same,))
    assert list(snap.economic_receivables) == ["stl:a"]
    assert [d.id for d in snap.economic_receivables["stl:a"].evidence] == ["stl:b"]
    assert snap.counted_total() == 1000


def test_incremental_equals_full_when_provenance_changes():
    """A re-read of the first document (new version, later registration time) can change
    which copy is the basis: the incremental recompute must follow."""
    a, b = _line("stl:a", "PO-1"), _line("stl:b", "PO-1")
    pay = BankTxn(
        id="bank:1",
        tenant_id=T,
        booked_date=date(2025, 10, 20),
        amount=Money(1000),
        counterparty="가나유통",
    )
    s1 = _snap([a, b], {"stl:a": ("dv-1", T1), "stl:b": ("dv-2", T2)}, bank_txns=(pay,))
    r1 = full_recompute(s1)
    later = datetime(2025, 9, 3, tzinfo=UTC)
    change = Change(ChangeKind.UPDATE, "fact", "f:stl:a", _fact("f:stl:a", "stl:a", "dv-3", later))
    s2 = s1.apply(change)
    assert s2.economic_receivables["stl:a"].counted is False  # now the later copy
    inc, _plan, _groups = incremental_recompute(r1, s2, [change])
    full = full_recompute(s2)
    assert {k: v.result_hash for k, v in inc.decisions.items()} == {
        k: v.result_hash for k, v in full.decisions.items()
    }
