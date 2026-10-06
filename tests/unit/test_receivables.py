"""Economic receivables vs document rows (review finding F4) and the contractual term.

Hand-written fixtures; not evaluation data.
"""

import dataclasses
from datetime import UTC, date, datetime

import pytest
from jt_unit_helpers import T, agreement, inv, line, snapshot, txn

from jettae.domain import Change, ChangeKind, Money, ReconcileStatus, SourceSpan
from jettae.domain.errors import DomainValidationError
from jettae.domain.models import (
    Agreement,
    EvidenceLink,
    Fact,
    LinkRelation,
    ReceivableBasis,
    ReceivableState,
)
from jettae.domain.status import LineKind, TradeType
from jettae.evidence import full_recompute, incremental_recompute
from jettae.rules.contract_term import NO_INTEREST_NOTE, compute_contractual_due

PAID = date(2025, 10, 20)


def link(lid, invoice, relation, line_id=None):
    return EvidenceLink(lid, T, invoice, relation, line_id, confirmed_by="u1")


def balance(result) -> int:
    return sum(d.computation("recon").outputs["open"].amount for d in result.decisions.values())


def counted(result) -> list[str]:
    return sorted(
        d.subject_id
        for d in result.decisions.values()
        if d.computation("evidence").outputs["counted"]
    )


# ----------------------------------------------------------------- explicit reference
def test_settlement_line_is_basis_and_invoice_corroborates_by_reference():
    snap = snapshot(
        lines=[line("S1", 10_000, ref="PO-1")],
        invoices=[inv("I1", 10_000, ref="po 1")],  # normalised reference equality
        txns=[txn("T1", 10_000, PAID, ref="PO-1")],
    )
    assert list(snap.receivables) == ["S1"]
    model = snap.economic_receivables["S1"]
    assert model.basis is ReceivableBasis.SETTLEMENT_LINE and model.counted
    assert [e.id for e in model.evidence] == ["I1"]
    assert set(snap.documents) == {"S1", "I1"}
    assert snap.counted_total() == 10_000

    res = full_recompute(snap)
    assert list(res.decisions) == ["dec:S1"]
    d = res.decisions["dec:S1"]
    assert d.status is ReconcileStatus.MATCHED
    ev = d.computation("evidence").outputs
    assert ev["basis"] == "settlement_line" and ev["basis_id"] == "S1"
    assert [(x["id"], x["role"], x["method"]) for x in ev["documents"]] == [
        ("S1", "basis", None),
        ("I1", "corroborating", "reference"),
    ]
    assert ev["amount_conflict"] is False and ev["confirmation_required"] is None
    assert "f-I1" in d.facts_used  # the invoice is evidence of this decision
    assert res.graph.dependents("rec:invoice:I1") == {"dec:S1"}
    assert balance(res) == 0


def test_payment_quoting_the_invoice_number_pays_the_settlement_receivable():
    approval = "20250807-41000000-12345678"
    snap = snapshot(
        lines=[line("S1", 10_000, ref="PO-1001"), line("S2", 10_000, ref="PO-1002")],
        invoices=[inv("I1", 10_000, ref=approval)],
        links=[link("L1", "I1", LinkRelation.SAME_SALE, "S1")],
        # S1 and S2 have the same amount; only the invoice number in the memo (a reference
        # of S1's corroborating invoice) identifies the paid sale
        txns=[txn("T1", 10_000, PAID, memo=f"세금계산서 {approval}")],
    )
    res = full_recompute(snap)
    assert res.decisions["dec:S1"].status is ReconcileStatus.MATCHED
    assert res.decisions["dec:S2"].status is ReconcileStatus.UNMATCHED


def test_same_sale_in_any_upload_order_is_one_receivable():
    docs = [
        Change(ChangeKind.ADD, "invoice", "I1", inv("I1", 10_000, ref="PO-1")),
        Change(ChangeKind.ADD, "settlement_line", "S1", line("S1", 10_000, ref="PO-1")),
        Change(ChangeKind.ADD, "bank_txn", "T1", txn("T1", 10_000, PAID, ref="PO-1")),
    ]
    finals = []
    for order in ([0, 1, 2], [1, 0, 2], [2, 0, 1], [2, 1, 0]):
        snap = snapshot()
        prev = full_recompute(snap)
        totals = []
        for k in order:
            snap = snap.apply(docs[k])
            prev, plan, _ = incremental_recompute(prev, snap, [docs[k]])
            assert not plan.fallback_full
            assert prev.comparable() == full_recompute(snap).comparable()
            totals.append(snap.counted_total())
        assert max(totals) == 10_000  # never two receivables for the one sale
        finals.append(prev.comparable())
        assert balance(prev) == 0
    assert all(f == finals[0] for f in finals)


# ----------------------------------------------------------------- never by amount alone
def test_equal_amount_and_date_documents_are_never_merged():
    # settlement line + invoice without a shared reference: uncertain -> confirmation item
    snap = snapshot(lines=[line("S1", 10_000, ref="PO-1")], invoices=[inv("I1", 10_000)])
    res = full_recompute(snap)
    pend = res.decisions["dec:I1"]
    assert pend.status is ReconcileStatus.AMBIGUOUS
    assert "evidence_link" in pend.unresolved and pend.required_documents
    ev = pend.computation("evidence").outputs
    assert ev["counted"] is False and ev["state"] == "needs_confirmation"
    assert ev["confirmation_required"]["candidate_settlement_lines"] == ["S1"]
    assert pend.computation("recon").outputs["counted"] is False
    assert pend.computation("due") is None and pend.allocations == ()
    assert counted(res) == ["S1"] and snap.counted_total() == 10_000
    # the settlement receivable has no corroborating evidence (not merged), only a candidate
    s1 = res.decisions["dec:S1"].computation("evidence").outputs
    assert [x["id"] for x in s1["documents"]] == ["S1"]
    assert [x["id"] for x in s1["candidates"]] == ["I1"]

    # two settlement lines / two invoices of the same amount and date are two sales
    two_lines = snapshot(lines=[line("S1", 10_000, ref="PO-1"), line("S2", 10_000, ref="PO-2")])
    assert two_lines.counted_total() == 20_000
    two_invoices = snapshot(invoices=[inv("I1", 10_000), inv("I2", 10_000)])
    assert sorted(two_invoices.receivables) == ["I1", "I2"]
    assert two_invoices.counted_total() == 20_000


def test_invoice_without_settlement_lines_is_its_own_receivable():
    snap = snapshot(
        invoices=[inv("I1", 10_000, ref="PO-1")],
        lines=[line("S9", 5_000, ref="PO-1", cp="다라상사")],  # other counterparty
    )
    res = full_recompute(snap)
    ev = res.decisions["dec:I1"].computation("evidence").outputs
    assert ev["basis"] == "invoice" and ev["counted"] is True
    assert snap.counted_total() == 15_000


def test_reference_matching_several_lines_needs_confirmation():
    snap = snapshot(
        lines=[line("S1", 6_000, ref="PO-1"), line("S2", 4_000, ref="PO-1")],
        invoices=[inv("I1", 10_000, ref="PO-1")],
    )
    model = snap.economic_receivables["I1"]
    assert model.state is ReceivableState.NEEDS_CONFIRMATION
    assert [c.id for c in model.candidates] == ["S1", "S2"]
    assert snap.counted_total() == 10_000


def test_deduction_lines_are_never_linked_to_invoices():
    snap = snapshot(
        lines=[line("D1", -1_000, ref="PO-1", kind=LineKind.DEDUCTION)],
        invoices=[inv("I1", 10_000, ref="PO-1")],
    )
    assert sorted(snap.receivables) == ["D1", "I1"]
    assert snap.economic_receivables["I1"].counted


# ----------------------------------------------------------------- conflict
def test_linked_documents_with_different_amounts_conflict_and_show_both():
    snap = snapshot(lines=[line("S1", 10_000, ref="PO-1")], invoices=[inv("I1", 9_000, ref="PO-1")])
    res = full_recompute(snap)
    d = res.decisions["dec:S1"]
    assert list(res.decisions) == ["dec:S1"]
    assert d.status is ReconcileStatus.CONFLICT
    assert "evidence_amount_conflict" in d.unresolved and d.required_documents
    assert any("[S1] 10,000원" in a and "[I1] 9,000원" in a for a in d.assumptions)
    ev = d.computation("evidence").outputs
    assert ev["amount_conflict"] is True
    assert {x["id"]: x["amount"].amount for x in ev["documents"]} == {"S1": 10_000, "I1": 9_000}
    assert d.computation("due") is None
    assert snap.counted_total() == 10_000  # basis amount; the invoice never adds


# ----------------------------------------------------------------- user confirmation
def test_user_confirmation_links_or_separates_and_invalidates_incrementally():
    s0 = snapshot(lines=[line("S1", 10_000, ref="PO-1")], invoices=[inv("I1", 10_000)])
    r0 = full_recompute(s0)
    assert "dec:I1" in r0.decisions

    same = link("L1", "I1", LinkRelation.SAME_SALE, "S1")
    ch = Change(ChangeKind.ADD, "evidence_link", "L1", same)
    s1 = s0.apply(ch)
    r1, plan, groups = incremental_recompute(r0, s1, [ch])
    assert not plan.fallback_full and groups == {"가나유통"}
    assert "dec:I1" in plan.removed
    assert r1.comparable() == full_recompute(s1).comparable()
    assert list(r1.decisions) == ["dec:S1"]
    docs = r1.decisions["dec:S1"].computation("evidence").outputs["documents"]
    assert docs[1]["method"] == "user_confirmation"
    assert r1.graph.dependents("rec:evidence_link:L1") == {"dec:S1"}

    sep = link("L1", "I1", LinkRelation.SEPARATE_SALE)
    ch2 = Change(ChangeKind.UPDATE, "evidence_link", "L1", sep)
    s2 = s1.apply(ch2)
    r2, plan2, _ = incremental_recompute(r1, s2, [ch2])
    assert not plan2.fallback_full
    assert r2.comparable() == full_recompute(s2).comparable()
    assert counted(r2) == ["I1", "S1"] and s2.counted_total() == 20_000


def test_conflicting_or_invalid_confirmations_stay_unconfirmed():
    lines = [line("S1", 10_000, ref="PO-1"), line("S9", 10_000, ref="PO-9", cp="다라상사")]
    both = snapshot(
        lines=lines,
        invoices=[inv("I1", 10_000)],
        links=[
            link("L1", "I1", LinkRelation.SAME_SALE, "S1"),
            link("L2", "I1", LinkRelation.SEPARATE_SALE),
        ],
    )
    assert not both.economic_receivables["I1"].counted
    other_cp = snapshot(
        lines=lines, invoices=[inv("I1", 10_000)], links=[link("L1", "I1", "same_sale", "S9")]
    )
    model = other_cp.economic_receivables["I1"]
    assert not model.counted and "S9" in model.notes[0]


def test_evidence_link_validation():
    with pytest.raises(DomainValidationError):
        EvidenceLink("L1", T, "I1", LinkRelation.SAME_SALE)
    with pytest.raises(DomainValidationError):
        EvidenceLink("L1", T, "I1", LinkRelation.SEPARATE_SALE, "S1")


# ----------------------------------------------------------------- contractual due
def _with_term_fact(snap, ag: Agreement, span: SourceSpan):
    f = Fact(
        id=f"fact:{ag.id}:payment_term_days",
        tenant_id=T,
        kind="agreement.payment_term_days",
        value=ag.payment_term_days,
        span=span,
        extractor="test",
        observed_at=datetime(2025, 11, 1, tzinfo=UTC),
        subject_id=ag.id,
    )
    ag2 = dataclasses.replace(ag, facts=(f.id,))
    return dataclasses.replace(snap, agreements=(ag2,), facts=(*snap.facts, f))


def test_contractual_due_is_separate_and_never_uses_the_statutory_rate():
    ag = agreement("A1", trade_type=TradeType.DIRECT, payment_term_days=30)
    span = SourceSpan("docv-ag", {"sheet": "약정", "row": 2, "col": "C"}, "30")
    snap = _with_term_fact(snapshot(lines=[line("S1", 10_000_000, ref="PO-1")]), ag, span)
    d = full_recompute(snap).decisions["dec:S1"]
    c = d.computation("contractual_due")
    o = c.outputs
    assert c.inputs["payment_term_days"] == 30
    assert o["base_date"] == date(2025, 8, 7) and o["due_date"] == date(2025, 9, 6)
    assert o["is_business_day"] is False  # Saturday: no rollover applied to the contract date
    assert o["interest"] is None and o["interest_note"] == NO_INTEREST_NOTE
    assert o["source"] == [
        {"agreement_id": "A1", "fact_id": "fact:A1:payment_term_days", "span": span}
    ]
    statutory = {v["label"]: v["due_date"] for v in d.computation("due").outputs["variants"]}
    for cmp_ in o["statutory_comparison"]:
        assert cmp_["difference_days"] == (date(2025, 9, 6) - statutory[cmp_["label"]]).days
    # statutory interest is still the only interest; the contractual term adds none
    assert "contract_term_base" in d.unresolved
    assert NO_INTEREST_NOTE in d.assumptions


def test_contractual_due_conflict_and_missing_base():
    a1 = agreement("A1", payment_term_days=30)
    a2 = agreement("A2", payment_term_days=45)
    d = full_recompute(snapshot(invoices=[inv("I1", 1_000)], agreements=[a1, a2])).decisions[
        "dec:I1"
    ]
    o = d.computation("contractual_due").outputs
    assert o["conflict"] is True and o["values"] == [30, 45] and o["interest"] is None
    assert "contract_term_conflict" in d.unresolved

    no_base = snapshot(invoices=[inv("I1", 1_000, received=None)], agreements=[a1])
    d2 = full_recompute(no_base).decisions["dec:I1"]
    assert d2.status is ReconcileStatus.INSUFFICIENT_EVIDENCE
    assert d2.computation("contractual_due").outputs["insufficient"] is True


def test_compute_contractual_due_unit():
    r = compute_contractual_due(30, "consignment", date(2025, 8, 31))
    assert r.due_date == date(2025, 9, 30) and r.interest is None
    assert r.base_field == "sales_close_date"
    assert compute_contractual_due(30, None, date(2025, 8, 31)).missing == ("trade_type",)
    with pytest.raises(ValueError):
        compute_contractual_due(-1, "direct", date(2025, 8, 31))


def test_money_amounts_are_integers_in_evidence_outputs():
    snap = snapshot(
        lines=[line("S1", 10_000, ref="PO-1")], invoices=[inv("I1", 10_000, ref="PO-1")]
    )
    ev = full_recompute(snap).decisions["dec:S1"].computation("evidence").outputs
    assert all(isinstance(x["amount"], Money) for x in ev["documents"])
