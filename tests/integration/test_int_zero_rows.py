"""F5: a verified empty revision replaces the document's rows; a parse failure (or a file
without a recognisable table) never deletes existing data. Real parser -> worker -> ledger."""

from __future__ import annotations

import pytest

HEADER = "거래처,거래형태,발주번호,정산금액,상품수령일\n"
V1 = HEADER + "가나유통,직매입,PO-1,1000,2025-08-07\n가나유통,직매입,PO-2,700,2025-08-08\n"


_ROWS = [HEADER.strip()] + [f"가나유통,직매입,PO-{i},{1000 + i},2025-08-07" for i in range(400)]
_TEXT = "\n".join(_ROWS)
CORRUPT = (_TEXT[:9000] + "\x00" + _TEXT[9000:]).encode("utf-8")


def lines(rt, tenant):
    return rt.repos.ledger.list(tenant, "settlement_line")


def test_valid_zero_row_revision_removes_and_reports(api, rt, tenant, work):
    v1 = api.upload(V1)
    work()
    assert len(lines(rt, tenant)) == 2
    facts_before = len(rt.repos.facts.list(tenant))
    assert facts_before > 0
    v2 = api.upload(HEADER, document_id=v1["document"]["document_id"])
    work()
    res = api.job(v2["job_id"])["result"]
    app = res["application"]
    assert res["document_status"] == "PARSED"
    assert app["state"] == "applied_empty" and app["records_removed"] == 2
    assert "2건" in app["message"] and app["current_version"] == 2
    assert res["counts"] == {
        "tables_recognized": 1,
        "source_rows": 0,
        "applied_rows": 0,
        "excluded_rows": 0,
        "tables_unread": 0,
        "unread_rows": 0,
    }
    assert lines(rt, tenant) == []
    assert rt.repos.facts.list(tenant) == []  # the document's facts went with its rows


@pytest.mark.parametrize(
    ("content", "expected_state"),
    [
        # a NUL byte deep inside the text: the upload check passes, the parser fails
        (CORRUPT, "not_applied_parse"),
        # text without any table header: mapping would be needed
        ("정산 안내문입니다. 표가 없습니다.\n".encode(), "not_applied_parse"),
        # recognised header, no data rows, but the 합계 row is not zero: unverified
        ((HEADER + "합계,,,1700,\n").encode(), "not_applied_empty_unverified"),
    ],
    ids=["corrupt", "no_table", "empty_with_total"],
)
def test_parse_failure_keeps_existing_rows(api, rt, tenant, work, content, expected_state):
    v1 = api.upload(V1)
    work()
    before = sorted(ln.id for ln in lines(rt, tenant))
    v2 = api.upload(content, document_id=v1["document"]["document_id"])
    work()
    job = api.job(v2["job_id"])
    assert job["status"] == "succeeded", job
    app = job["result"]["application"]
    assert app["state"] == expected_state, app
    assert app["current_version"] == 1 and app["records_removed"] == 0
    assert "기존 기록은 그대로" in app["message"]
    assert sorted(ln.id for ln in lines(rt, tenant)) == before
    # the later good revision still replaces v1 normally
    v3 = api.upload(
        HEADER + "가나유통,직매입,PO-1,1000,2025-08-07\n", document_id=v1["document"]["document_id"]
    )
    work()
    app3 = api.job(v3["job_id"])["result"]["application"]
    assert app3["state"] == "applied" and app3["records_removed"] == 1
    assert [ln.reference for ln in lines(rt, tenant)] == ["PO-1"]
