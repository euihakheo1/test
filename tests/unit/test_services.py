import threading
from datetime import date

import pytest
from jt_unit_helpers import NOW, T, agreement, fact, inv, txn

from jettae.app import JettaeService, csv_safe, in_memory_repositories
from jettae.domain import (
    Change,
    ChangeKind,
    DocKind,
    NotFoundError,
    ReconcileStatus,
    ReportValidityError,
    ReviewStatus,
    StaleResultError,
    TenantMismatchError,
)


@pytest.fixture
def svc():
    s = JettaeService(in_memory_repositories(), clock=lambda: NOW)
    doc = s.register_document(
        T,
        filename="bank.csv",
        content=b"date,amount\n2025-10-20,10000000\n",
        media_type="text/csv",
        kind=DocKind.BANK,
        text="2025-10-20,10000000",
    )
    i1 = inv("I1", 10_000_000)
    t1 = txn("T1", 10_000_000, date(2025, 10, 20))
    facts = [fact("f-I1", "I1", 10000000, doc=doc.id), fact("f-T1", "T1", 10000000, doc=doc.id)]
    s.record_facts(T, facts=facts, records=[i1, t1])
    s.run_analysis(T, as_of=date(2025, 11, 1))
    return s


def test_run_analysis_views_are_verified(svc):
    v = svc.decision_view(T, "dec:I1")
    assert v.decision.status is ReconcileStatus.MATCHED
    assert v.review_status is ReviewStatus.VERIFIED, [c for c in v.checks if not c.passed]
    assert {c.name for c in v.checks} >= {"numbers", "wording", "citation"}
    assert "59,452원" in v.explanation and "42,465원" in v.explanation


def test_approve_then_change_requires_review_and_keeps_history(svc):
    d = svc.repos.decisions.get_current(T, "dec:I1")
    appr = svc.approve(T, "dec:I1", d.result_hash, "kim")
    assert svc.decision_view(T, "dec:I1").review_status is ReviewStatus.APPROVED
    out = svc.apply_change(
        T, [Change(ChangeKind.ADD, "agreement", "A1", agreement("A1"))], verify_full=True
    )
    assert out.equivalent_to_full is True
    assert out.review_required == ("dec:I1",)
    assert not out.plan.fallback_full
    view = svc.decision_view(T, "dec:I1")
    assert view.review_status is ReviewStatus.REVIEW_REQUIRED
    assert view.approvals == (appr,)  # approval kept, never overwritten
    assert len(svc.repos.decisions.history(T, "dec:I1")) == 2
    with pytest.raises(StaleResultError):
        svc.approve(T, "dec:I1", d.result_hash, "lee")
    svc.approve(T, "dec:I1", view.decision.result_hash, "lee")
    assert svc.decision_view(T, "dec:I1").review_status is ReviewStatus.APPROVED


def test_approval_race_with_change_is_consistent(svc):
    for k in range(10):
        cur = svc.repos.decisions.get_current(T, "dec:I1")
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def approver(h=cur.result_hash, barrier=barrier, errors=errors):
            barrier.wait()
            try:
                svc.approve(T, "dec:I1", h, "kim")
            except StaleResultError as e:
                errors.append(e)

        def changer(k=k, barrier=barrier):
            barrier.wait()
            t = txn(f"TX{k}", 1, date(2025, 10, 25))
            svc.apply_change(T, [Change(ChangeKind.ADD, "bank_txn", t.id, t)])

        th = [threading.Thread(target=approver), threading.Thread(target=changer)]
        for x in th:
            x.start()
        for x in th:
            x.join()
        now = svc.repos.decisions.get_current(T, "dec:I1")
        assert now.result_hash != cur.result_hash
        status = svc.decision_view(T, "dec:I1").review_status
        approvals = svc.repos.approvals.list_for(T, "dec:I1")
        # never APPROVED for a result nobody approved
        assert status is ReviewStatus.REVIEW_REQUIRED or not approvals
        assert all(a.result_hash != now.result_hash for a in approvals)
        assert len(errors) in (0, 1)


def test_export_report_rechecks_validity(svc):
    d = svc.repos.decisions.get_current(T, "dec:I1")
    with pytest.raises(ReportValidityError):
        svc.export_report(T, ["dec:I1"], require_approved=True)
    svc.approve(T, "dec:I1", d.result_hash, "kim")
    rep = svc.export_report(T, ["dec:I1"], require_approved=True)
    assert rep.all_approved and rep.items[0].sources
    svc.apply_change(T, [Change(ChangeKind.ADD, "agreement", "A1", agreement("A1"))])
    rep2 = svc.export_report(T, ["dec:I1"])
    assert not rep2.all_approved
    assert rep2.items[0].review_status is ReviewStatus.REVIEW_REQUIRED
    assert csv_safe("=SUM(A1)") == "'=SUM(A1)"
    assert rep2.to_rows()[1][0] == "dec:I1"


def test_required_documents_listing(svc):
    i2 = inv("I2", 300_000, cp="다라상사", received=None, issue=date(2025, 9, 1))
    out = svc.apply_change(T, [Change(ChangeKind.ADD, "invoice", "I2", i2)])
    assert out.changed == ("dec:I2",)
    req = {r.decision_id: r for r in svc.list_required_documents(T)}
    assert req["dec:I2"].status is ReconcileStatus.INSUFFICIENT_EVIDENCE
    assert req["dec:I2"].missing == ("goods_received_date",)
    assert "dec:I1" in req  # rollover unresolved -> listed as condition to confirm
    assert req["dec:I1"].unresolved == ("rollover",)


def test_removed_subject_is_superseded(svc):
    svc.apply_change(T, [Change(ChangeKind.REMOVE, "invoice", "I1")])
    assert svc.decision_view(T, "dec:I1").review_status is ReviewStatus.SUPERSEDED
    with pytest.raises(NotFoundError):
        svc.approve(T, "dec:I1", "x", "kim")


def test_tenant_isolation(svc):
    with pytest.raises(TenantMismatchError):
        svc.record_facts(T, records=[inv("Z", 1, tenant="t2")])
    assert svc.repos.decisions.current("t2") == {}
    with pytest.raises(NotFoundError):
        svc.decision_view("t2", "dec:I1")
    with pytest.raises(TenantMismatchError):
        svc.apply_change(T, [Change(ChangeKind.ADD, "invoice", "Z", inv("Z", 1, tenant="t2"))])


def test_document_versions():
    s = JettaeService(in_memory_repositories(), clock=lambda: NOW)
    a = s.register_document(
        T, filename="x.csv", content=b"1", media_type="text/csv", document_id="D"
    )
    same = s.register_document(
        T, filename="x.csv", content=b"1", media_type="text/csv", document_id="D"
    )
    b = s.register_document(
        T, filename="x.csv", content=b"2", media_type="text/csv", document_id="D"
    )
    assert same.id == a.id
    assert b.version == 2 and b.supersedes == a.id
    assert s.repos.blobs.get(T, b.storage_key) == b"2"
    assert s.repos.blobs.get("t2", b.storage_key) is None
