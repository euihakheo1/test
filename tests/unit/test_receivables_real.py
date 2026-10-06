"""F4 + contractual term through the real parser and application service (in-memory repos).

The CSV contents are tiny hand-written fixtures (not evaluation data).
"""

from datetime import UTC, date, datetime

import pytest

from jettae.app import JettaeService, in_memory_repositories
from jettae.domain.status import ReconcileStatus
from jettae.evidence import AnalysisConfig
from jettae.ingest import ingest_document

NOW = datetime(2025, 11, 1, tzinfo=UTC)
TEN = "t-real"
APPROVAL = "20250807-41000000-12345678"
HOMETAX = (
    "작성일자,승인번호,공급받는자상호,합계금액,공급가액,세액\n"
    f"2025-08-07,{APPROVAL},(주)가나유통,11000000,10000000,1000000\n"
).encode()
# the retailer statement quotes the tax-invoice approval number as its 거래번호
SETTLE_WITH_INVOICE_NO = (
    f"거래처,거래형태,거래번호,정산금액,상품수령일\n가나유통,직매입,{APPROVAL},11000000,2025-08-07\n"
).encode()
SETTLE_WITH_PO = (
    "거래처,거래형태,발주번호,정산금액,상품수령일\n가나유통,직매입,PO-1001,11000000,2025-08-07\n"
).encode()
BANK = (
    "거래일자,적요,보낸분/받는분,출금액,입금액,잔액\n"
    '2025-10-20,타행입금,가나유통,0,"11,000,000","11,000,000"\n'
).encode("cp949")
AGREEMENT = "거래처,거래형태,지급기한,계약시작일\n가나유통,직매입,30,2025-01-01\n".encode()


@pytest.fixture
def svc():
    return JettaeService(in_memory_repositories(), clock=lambda: NOW)


def _ingest(svc, files):
    for name, content, doc_id in files:
        doc, _, _ = ingest_document(svc, TEN, filename=name, content=content, document_id=doc_id)
        assert doc.status.value == "PARSED", (name, doc.status)


def _decisions(svc):
    svc.run_analysis(TEN, as_of=date(2025, 11, 1))
    return {v.decision.subject_id: v.decision for v in svc.list_decisions(TEN)}


@pytest.mark.parametrize("reverse", [False, True])
def test_settlement_and_invoice_of_one_sale_are_one_receivable(svc, reverse):
    files = [
        ("정산서.csv", SETTLE_WITH_INVOICE_NO, "s1"),
        ("매출세금계산서.csv", HOMETAX, "h1"),
        ("입금.csv", BANK, "b1"),
    ]
    _ingest(svc, list(reversed(files)) if reverse else files)
    decs = _decisions(svc)
    (line,) = svc.repos.ledger.list(TEN, "settlement_line")
    (invoice,) = svc.repos.ledger.list(TEN, "invoice")
    assert list(decs) == [line.id]  # one decision; the invoice is corroborating evidence
    d = decs[line.id]
    assert d.status is ReconcileStatus.MATCHED
    assert d.computation("recon").outputs["open"].amount == 0
    ev = d.computation("evidence").outputs
    assert ev["basis"] == "settlement_line"
    assert [x["id"] for x in ev["documents"]] == [line.id, invoice.id]
    snap = svc.snapshot(TEN, AnalysisConfig(as_of=date(2025, 11, 1)))
    assert snap.counted_total() == 11_000_000


def test_unreferenced_invoice_is_not_counted_until_confirmed(svc):
    _ingest(svc, [("정산서.csv", SETTLE_WITH_PO, "s1"), ("매출세금계산서.csv", HOMETAX, "h1")])
    decs = _decisions(svc)
    (line,) = svc.repos.ledger.list(TEN, "settlement_line")
    (invoice,) = svc.repos.ledger.list(TEN, "invoice")
    pend = decs[invoice.id]
    assert pend.status is ReconcileStatus.AMBIGUOUS and "evidence_link" in pend.unresolved
    conf = pend.computation("evidence").outputs["confirmation_required"]
    assert conf["candidate_settlement_lines"] == [line.id]
    total_open = sum(x.computation("recon").outputs["open"].amount for x in decs.values())
    assert total_open == 11_000_000  # the settlement line only, not 22,000,000


def test_contractual_due_from_ingested_agreement_cites_its_source(svc):
    _ingest(svc, [("약정.csv", AGREEMENT, "a1"), ("정산서.csv", SETTLE_WITH_PO, "s1")])
    (line,) = svc.repos.ledger.list(TEN, "settlement_line")
    (ag,) = svc.repos.ledger.list(TEN, "agreement")
    assert ag.payment_term_days == 30
    d = _decisions(svc)[line.id]
    o = d.computation("contractual_due").outputs
    assert o["due_date"] == date(2025, 9, 6) and o["interest"] is None
    (src,) = o["source"]
    assert src["agreement_id"] == ag.id and src["span"] is not None
    assert src["span"].excerpt.strip() == "30"
    doc_ids = {dv.id for dv in svc.repos.documents.list(TEN)}
    assert src["span"].doc_version_id in doc_ids
