"""The in-process path (``jettae.ingest.pipeline.ingest_document``: CLI / demo) applies
documents through the same :class:`DocumentApplier` as the worker: same version rule,
zero-row rule, parse-failure rule and partial-application policy."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest
from openpyxl import Workbook

from jettae.app import JettaeService, in_memory_repositories
from jettae.app.contracts import FORMAT_IDS, ApplyState
from jettae.app.doc_apply import DocumentAckRequiredError, DocumentApplier
from jettae.domain.status import DocumentStatus
from jettae.ingest import ingest_document
from jettae.ingest.formats import FORMATS
from jettae.ingest.pipeline import parse_document

T = "t-inproc"
NOW = datetime(2025, 11, 1, tzinfo=UTC)
HEADER = "거래처,거래형태,발주번호,정산금액,상품수령일\n"


@pytest.fixture
def svc() -> JettaeService:
    return JettaeService(in_memory_repositories(), clock=lambda: NOW)


def csv(*rows: str) -> bytes:
    return (HEADER + "".join(r + "\n" for r in rows)).encode()


def refs(svc: JettaeService) -> list[str]:
    return sorted(ln.reference or "" for ln in svc.repos.ledger.list(T, "settlement_line"))


def test_contract_format_ids_match_the_parser():
    assert set(FORMAT_IDS) == {f.id for f in FORMATS}


def test_versions_zero_rows_and_failures_follow_the_shared_rules(svc):
    d1, r1, _ = ingest_document(
        svc, T, filename="s.csv", content=csv("가나유통,직매입,PO-1,1000,2025-08-07")
    )
    assert r1.application is not None and r1.application.state is ApplyState.APPLIED
    d2, r2, _ = ingest_document(
        svc,
        T,
        filename="s2.csv",
        content=csv("가나유통,직매입,PO-2,2000,2025-08-07"),
        document_id=d1.document_id,
    )
    assert r2.application.state is ApplyState.APPLIED and r2.application.records_removed == 1
    assert refs(svc) == ["PO-2"]

    # re-applying v1's parse (a late or retried run) is not promoted
    old = parse_document(
        csv("가나유통,직매입,PO-1,1000,2025-08-07"),
        filename="s.csv",
        tenant_id=T,
        doc_version_id=d1.id,
        document_key=d1.document_id,
        observed_at=d1.created_at,
    )
    report = DocumentApplier(svc).apply(T, d1, old)
    assert report.outcome.state is ApplyState.NOT_PROMOTED_OLDER and report.changes == ()
    assert refs(svc) == ["PO-2"]

    # a workbook without any table is not an empty revision
    wb = Workbook()
    buf = io.BytesIO()
    wb.save(buf)
    _, r3, ch3 = ingest_document(
        svc, T, filename="empty.xlsx", content=buf.getvalue(), document_id=d1.document_id
    )
    assert r3.status is DocumentStatus.PARSED and r3.counts.tables_recognized == 0
    assert r3.application.state is ApplyState.NOT_APPLIED_NO_TABLE and ch3 == []
    assert refs(svc) == ["PO-2"]

    # a recognised table with zero rows is
    _, r4, _ = ingest_document(svc, T, filename="h.csv", content=csv(), document_id=d1.document_id)
    assert r4.application.state is ApplyState.APPLIED_EMPTY
    assert r4.application.records_removed == 1 and refs(svc) == []


def test_partial_document_blocks_approval_in_process(svc):
    content = csv(
        "가나유통,직매입,PO-1,1000,2025-08-07",
        "가나유통,직매입,PO-2,잘못된금액,2025-08-07",
        "가나유통,직매입,PO-3,300,2025-08-07",
        "가나유통,직매입,PO-4,400,2025-08-07",
    )
    doc, res, _ = ingest_document(svc, T, filename="s.csv", content=content)
    out = res.application
    assert out.state is ApplyState.APPLIED_NEEDS_ACK and out.ack_required
    assert res.counts.excluded_rows == 1
    svc.run_analysis(T, as_of=NOW.date())
    view = next(v for v in svc.list_decisions(T) if v.decision.subject_id.endswith("#1"))
    with pytest.raises(DocumentAckRequiredError):
        svc.approve(T, view.decision.id, view.decision.result_hash, "a@x")
    DocumentApplier(svc).acknowledge(T, doc.id, out.fingerprint, "a@x")
    svc.approve(T, view.decision.id, view.decision.result_hash, "a@x")
