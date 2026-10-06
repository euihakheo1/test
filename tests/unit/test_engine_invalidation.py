import dataclasses
from datetime import date

import pytest
from jt_unit_helpers import agreement, fact, inv, snapshot, txn

from jettae.domain import Change, ChangeKind, ReconcileStatus, TenantMismatchError
from jettae.evidence import (
    DependencyGraph,
    QueryScope,
    full_recompute,
    incremental_recompute,
    plan,
)


def base_snap(**kw):
    return snapshot(
        invoices=[inv("I1", 10_000_000)],
        txns=[txn("T1", 10_000_000, date(2025, 10, 20))],
        **kw,
    )


def test_full_recompute_decision_content():
    res = full_recompute(base_snap())
    d = res.decisions["dec:I1"]
    assert d.status is ReconcileStatus.MATCHED
    assert "rollover" in d.unresolved  # due 2025-10-06 is 추석; no agreement fixes rollover
    due = d.computation("due")
    labels = {v["label"]: v for v in due.outputs["variants"]}
    assert labels["rollover_off"]["interest_total"].amount == 59452
    assert labels["rollover_on"]["interest_total"].amount == 42465
    assert any("적용 약정 없음" in a for a in d.assumptions)
    assert set(d.facts_used) == {"f-I1", "f-T1"}
    assert res.graph.dependents("doc:docv-1") == {"dec:I1"}


def test_new_agreement_invalidates_no_agreement_decision():
    s0 = base_snap()
    r0 = full_recompute(s0)
    ch = Change(ChangeKind.ADD, "agreement", "A1", agreement("A1", rollover=True))
    s1 = s0.apply(ch)
    p = plan(r0.graph, [ch], s1)
    assert not p.fallback_full
    assert "dec:I1" in p.affected
    assert any("agreements:가나유통" in r for r in p.reasons["dec:I1"])
    r1, p1, groups = incremental_recompute(r0, s1, [ch])
    assert groups == {"가나유통"}
    d1 = r1.decisions["dec:I1"]
    assert "rollover" not in d1.unresolved
    assert d1.result_hash != r0.decisions["dec:I1"].result_hash
    assert r1.comparable() == full_recompute(s1).comparable()


def test_agreement_for_other_counterparty_does_not_invalidate():
    s0 = base_snap()
    r0 = full_recompute(s0)
    ch = Change(ChangeKind.ADD, "agreement", "A9", agreement("A9", cp="다라상사"))
    p = plan(r0.graph, [ch], s0.apply(ch))
    assert p.affected == frozenset() and not p.fallback_full


def test_fact_and_document_changes_follow_direct_edges():
    s0 = base_snap()
    r0 = full_recompute(s0)
    new_fact = fact("f-I1", "I1", 9_999_999)
    ch = Change(ChangeKind.UPDATE, "fact", "f-I1", new_fact)
    s1 = s0.apply(ch)
    p = plan(r0.graph, [ch], s1)
    assert p.reasons["dec:I1"][0].startswith("fact f-I1")
    r1, _, _ = incremental_recompute(r0, s1, [ch])
    # evidence changed -> result hash changes even though the amounts did not
    assert r1.decisions["dec:I1"].result_hash != r0.decisions["dec:I1"].result_hash
    doc_ch = Change(ChangeKind.UPDATE, "doc_version", "docv-1")
    p2 = plan(r0.graph, [doc_ch], s0)
    assert p2.affected == {"dec:I1"}


def test_new_payment_and_new_invoice_in_other_group():
    s0 = base_snap()
    r0 = full_recompute(s0)
    ch1 = Change(ChangeKind.ADD, "invoice", "J1", inv("J1", 500_000, cp="다라상사"))
    s1 = s0.apply(ch1)
    p = plan(r0.graph, [ch1], s1)
    assert p.affected == {"dec:J1"}
    r1, _, groups = incremental_recompute(r0, s1, [ch1])
    assert groups == {"다라상사"}
    assert r1.comparable() == full_recompute(s1).comparable()
    ch2 = Change(ChangeKind.ADD, "bank_txn", "T2", txn("T2", 1, date(2025, 10, 21)))
    p2 = plan(r1.graph, [ch2], s1.apply(ch2))
    assert p2.affected == {"dec:I1"}


def test_removed_subject_and_unattributed_payment():
    s0 = base_snap()
    r0 = full_recompute(s0)
    ch = Change(ChangeKind.REMOVE, "invoice", "I1")
    s1 = s0.apply(ch)
    p = plan(r0.graph, [ch], s1)
    assert p.removed == {"dec:I1"}
    r1, _, _ = incremental_recompute(r0, s1, [ch])
    assert r1.decisions == {}
    assert [t for t, _ in r1.unattributed] == ["T1"]
    assert r1.comparable() == full_recompute(s1).comparable()


def test_unattributed_txn_is_attributed_by_reference():
    s = snapshot(
        invoices=[inv("I1", 1000, ref="INV-0042")],
        txns=[txn("T1", 1000, date(2025, 9, 1), cp="모르는이름", memo="INV-0042")],
    )
    r = full_recompute(s)
    assert r.decisions["dec:I1"].computation("recon").outputs["status"] is ReconcileStatus.MATCHED
    assert r.unattributed == ()


def test_untracked_or_unknown_dependencies_fall_back_to_full():
    s0 = base_snap()
    r0 = full_recompute(s0)
    p = plan(r0.graph, [Change(ChangeKind.ADD, "email", "e1")], s0)
    assert p.fallback_full and "dec:I1" in p.affected
    p2 = plan(DependencyGraph(), [], s0, existing_decisions=["dec:I1"])
    assert p2.fallback_full
    g = DependencyGraph()
    g.add_decision("dec:I1", [], {QueryScope("mystery", "x"): "h"})
    p3 = plan(g, [], s0)
    assert p3.fallback_full
    g2 = DependencyGraph()
    g2.add_decision("dec:I1", [], {}, tracked=False)
    assert plan(g2, [], s0).fallback_full
    assert plan(r0.graph, [], None).fallback_full
    r1, p4, _ = incremental_recompute(r0, s0, [Change(ChangeKind.ADD, "email", "e1")])
    assert p4.fallback_full and r1.comparable() == r0.comparable()


def test_config_change_invalidates_everything():
    s0 = base_snap()
    r0 = full_recompute(s0)
    cfg = dataclasses.replace(s0.config, rollover=True)
    ch = Change(ChangeKind.UPDATE, "config", "config", cfg)
    s1 = s0.apply(ch)
    p = plan(r0.graph, [ch], s1)
    assert p.affected == {"dec:I1"}
    r1, _, _ = incremental_recompute(r0, s1, [ch])
    assert r1.comparable() == full_recompute(s1).comparable()


def test_missing_base_date_decision_is_insufficient():
    s = snapshot(invoices=[inv("I1", 1000, received=None, issue=date(2025, 1, 5))])
    d = full_recompute(s).decisions["dec:I1"]
    assert d.status is ReconcileStatus.INSUFFICIENT_EVIDENCE
    assert d.missing == ("goods_received_date",)
    assert d.required_documents
    assert any("세금계산서" in a for a in d.assumptions)


def test_conflicting_agreements_give_conflict():
    s = snapshot(
        invoices=[inv("I1", 1000)],
        agreements=[agreement("A1", rollover=True), agreement("A2", rollover=False)],
    )
    d = full_recompute(s).decisions["dec:I1"]
    assert d.status is ReconcileStatus.CONFLICT
    assert "agreement_conflict" in d.unresolved


def test_graph_roundtrip_and_tenant_guard():
    r = full_recompute(base_snap())
    assert DependencyGraph.from_dict(r.graph.to_dict()) == r.graph
    with pytest.raises(TenantMismatchError):
        snapshot(invoices=[inv("X", 1, tenant="t2")])
