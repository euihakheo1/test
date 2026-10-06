"""The API/worker entrypoint contract (``jettae.ingest.pipeline.parse_document`` etc.)."""

import json

import pytest
from ing_helpers import TENANT, scanned_pdf

from jettae.app.contracts import MappingContractError
from jettae.domain.status import DocumentStatus
from jettae.ingest.pipeline import options_from_mapping, parse_document, suggest_mapping

BANK = (
    "거래일자,적요,보낸분/받는분,출금액,입금액,잔액\n"
    '2025-10-20,타행입금,가나유통,0,"10,000,000","10,000,000"\n'
).encode("cp949")
ODD = "날,돈,메모\n2025-10-20,1000,가나\n".encode()


def test_parse_document_parsed():
    out = parse_document(
        BANK,
        filename="b.csv",
        media_type="text/csv",
        kind="bank",
        tenant_id=TENANT,
        doc_version_id="dv1",
    )
    assert out.status is DocumentStatus.PARSED
    assert len(out.records) == 1 and out.records[0].tenant_id == TENANT
    assert out.facts and all(f.tenant_id == TENANT for f in out.facts)
    assert out.text and "가나유통" in out.text


def test_needs_mapping_suggestion_then_flat_mapping():
    out = parse_document(ODD, filename="x.csv", tenant_id=TENANT, doc_version_id="dv")
    assert out.status is DocumentStatus.NEEDS_MAPPING and out.records == ()
    assert out.reason
    s = suggest_mapping(ODD, filename="x.csv", media_type="text/csv", kind="other")
    json.dumps(s, ensure_ascii=False)  # JSON-compatible
    assert s["status"] == "NEEDS_MAPPING"
    # API mapping (columns by index): the only format with txn_date + deposit is the bank one
    out2 = parse_document(
        ODD,
        filename="x.csv",
        tenant_id=TENANT,
        doc_version_id="dv",
        mapping={"columns": {"txn_date": 0, "deposit": 1, "description": 2}},
    )
    assert out2.status is DocumentStatus.PARSED and len(out2.records) == 1
    assert out2.suggestion is not None and out2.suggestion["overall"] == "confirmed"


def test_options_from_mapping():
    o = options_from_mapping(
        {"columns": {"amount": 4}, "options": {"counterparty_override": "가나유통"}},
        kind="settlement",
    )
    assert o.counterparty_override == "가나유통" and o.format_id == "retail_settlement"
    assert o.mapping == {"*": {"amount": 4}}
    o2 = options_from_mapping({"columns": {"amount": 4}})  # bank and settlement have "amount"
    assert o2.format_id is None
    # a column for the counterparty is a column, never a company name
    o3 = options_from_mapping({"columns": {"counterparty": 1, "amount": 4}}, kind="settlement")
    assert o3.counterparty_override is None and o3.mapping == {
        "*": {"counterparty": 1, "amount": 4}
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"counterparty": 1, "amount": 4},  # retired flat shape (index vs. name ambiguity)
        {"columns": {"amount": "4"}},  # a string is not a column index
        {"columns": {"amount": True}},  # nor a boolean
        {"columns": {"amount": -1}},
        {"columns": {"amount": 1, "counterparty": 1}},  # one column for two fields
        {"options": {"counterparty_override": 1}},  # an override must be text
        {"options": {"counterparty_override": "   "}},
        {"options": {"unknown": "x"}},
        {"format_id": "nope"},
    ],
)
def test_mapping_contract_rejects(bad):
    with pytest.raises(MappingContractError):
        options_from_mapping(bad)


def test_bad_mapping_is_reported_not_raised():
    out = parse_document(
        ODD,
        filename="x.csv",
        tenant_id=TENANT,
        doc_version_id="dv",
        mapping={"format_id": "kr_bank_txn", "columns": {"txn_date": 9}},  # out of range
    )
    assert out.status is DocumentStatus.NEEDS_MAPPING
    assert any("mapping rejected" in n for n in out.notes)


def test_scan_through_bridge():
    out = parse_document(scanned_pdf(), filename="s.pdf", tenant_id=TENANT, doc_version_id="d")
    assert out.status is DocumentStatus.UNSUPPORTED_SCAN and out.reason


def test_db_bridge_accepts_outcome():
    bridge = pytest.importorskip("jettae.db.ingest_bridge")
    out = parse_document(BANK, filename="b.csv", tenant_id=TENANT, doc_version_id="dv1")
    norm = bridge.normalize_result(out)
    assert norm.status is DocumentStatus.PARSED and len(norm.records) == 1


def test_parse_result_boundary_checks():
    """A non-text counterparty, a PARSED result without row counts, or inconsistent counts
    never pass the parse contract (they would otherwise reach the ledger)."""
    from dataclasses import replace

    from pydantic import ValidationError

    from jettae.app.contracts import ParseResult, RowCounts
    from jettae.db.ingest_bridge import IngestContractError

    out = parse_document(BANK, filename="b.csv", tenant_id=TENANT, doc_version_id="dv1")
    (txn,) = out.records
    counts = RowCounts(tables_recognized=1, source_rows=1, applied_rows=1, excluded_rows=0)
    with pytest.raises(ValidationError, match="counterparty must be text"):
        ParseResult(status=out.status, records=(replace(txn, counterparty=1),), counts=counts)
    with pytest.raises(ValidationError, match="row counts"):
        ParseResult(status=out.status, records=out.records)
    with pytest.raises(ValidationError, match="applied_rows"):
        ParseResult(status=out.status, records=(), counts=counts)
    with pytest.raises(ValidationError, match="source_rows"):
        RowCounts(tables_recognized=1, source_rows=3, applied_rows=1, excluded_rows=1)
    with pytest.raises(IngestContractError):
        bridge_normalize({"status": "PARSED", "records": [replace(txn, counterparty=1)]})


def bridge_normalize(raw):
    from jettae.db.ingest_bridge import normalize_result

    return normalize_result(raw)


def test_issues_totals_and_counts_survive_the_bridge():
    content = (
        "거래처,거래형태,발주번호,정산금액,상품수령일\n"
        "가나유통,직매입,PO-1,1000,2025-08-07\n"
        "가나유통,직매입,PO-2,,2025-08-07\n"
        "가나유통,직매입,PO-3,300,2025-08-07\n"
        "가나유통,직매입,PO-4,400,2025-08-07\n"
        "합계,,,9999,\n"
    ).encode()
    out = parse_document(content, filename="s.csv", tenant_id=TENANT, doc_version_id="dv")
    norm = bridge_normalize(
        {
            "status": out.status.value,
            "records": out.records,
            "facts": out.facts,
            "issues": [i.model_dump() for i in out.issues],
            "totals": [t.model_dump() for t in out.totals],
            "counts": out.counts.model_dump(),
        }
    )
    assert norm.counts.excluded_rows == 1 and norm.counts.source_rows == 4
    assert [(i.row, i.field, i.kind.value) for i in norm.issues] == [(3, "amount", "excluded")]
    assert [(t.stated, t.computed, t.matches) for t in norm.totals] == [(9999, 1700, False)]
