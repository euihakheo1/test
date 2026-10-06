"""F3: row issues, excluded rows and table-total differences reach the job result, the
document detail and the approval policy (real parser -> worker -> API)."""

from __future__ import annotations

import pytest

HEADER = "거래처,거래형태,발주번호,정산금액,상품수령일\n"
GOOD = "가나유통,직매입,PO-1,1000,2025-08-07\n"
# more readable rows, so one bad cell does not make the header suggestion uncertain
MORE = "가나유통,직매입,PO-3,300,2025-08-09\n가나유통,직매입,PO-4,400,2025-08-10\n"
BANK = (
    "거래일자,적요,보낸분/받는분,출금액,입금액,잔액\n"
    '2025-10-20,타행입금,가나유통,0,"1,000","1,000"\n'
)

CASES = {
    # amount cell unreadable -> row excluded
    "amount_error": (
        GOOD + "가나유통,직매입,PO-2,금액오류,2025-08-07\n" + MORE,
        "amount",
        "excluded",
        1,
    ),
    # required value missing -> row excluded
    "missing_amount": (
        GOOD + "가나유통,직매입,PO-2,,2025-08-07\n" + MORE,
        "amount",
        "excluded",
        1,
    ),
    # base date unreadable -> row applied, date left empty (a value issue)
    "date_error": (
        GOOD + "가나유통,직매입,PO-2,500,2025-13-45\n" + MORE,
        "goods_received_date",
        "value",
        0,
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_row_issue_visible_and_blocks_approval_until_acknowledged(api, rt, tenant, work, case):
    rows, field, kind, excluded = CASES[case]
    up = api.upload(HEADER + rows)
    api.upload(BANK, kind="bank", name="bank.csv")
    work()
    res = api.job(up["job_id"])["result"]
    assert res["document_status"] == "PARSED"  # the format was recognised ...
    app = res["application"]
    assert app["state"] == "applied_needs_ack"  # ... but not every transaction was applied
    assert app["ack_required"] is True and app["acknowledged"] is False
    assert res["counts"] == {
        "tables_recognized": 1,
        "source_rows": 4,
        "applied_rows": 4 - excluded,
        "excluded_rows": excluded,
        "tables_unread": 0,
        "unread_rows": 0,
    }
    issues = [i for i in res["issues"] if i["row"] == 3]
    assert issues and issues[0]["field"] == field and issues[0]["kind"] == kind
    assert res["issues_total"] == len(res["issues"]) == 1
    # document detail carries the same information
    doc = api.version(up["document"]["id"])
    assert doc["apply_state"] == "applied_needs_ack" and doc["issue_count"] == 1
    assert doc["status_detail"]["issues"][0]["row"] == 3
    assert doc["status_detail"]["application"]["fingerprint"] == app["fingerprint"]

    # approval policy: decisions citing this document wait for the acknowledgment
    api.analyze()
    work()
    line = next(
        ln for ln in rt.repos.ledger.list(tenant, "settlement_line") if ln.reference == "PO-1"
    )
    d = api.decision(line.id)
    r = api.approve(line.id, d["result_hash"])
    assert r.status_code == 409, r.text
    body = r.json()["error"]
    assert body["code"] == "document_ack_required"
    assert body["details"]["documents"][0]["doc_version_id"] == up["document"]["id"]
    stale = api.acknowledge(up["document"]["id"], "0" * 64)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_fingerprint"
    ok = api.acknowledge(up["document"]["id"], app["fingerprint"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["acknowledged"] is True and ok.json()["ack_by"] == "int@x.example"
    assert api.approve(line.id, d["result_hash"]).status_code == 201


def test_total_row_mismatch_needs_acknowledgment(api, rt, tenant, work):
    up = api.upload(HEADER + GOOD + "합계,,,5000,\n")
    work()
    res = api.job(up["job_id"])["result"]
    assert res["application"]["state"] == "applied_needs_ack"
    assert res["counts"]["excluded_rows"] == 0 and res["issues"] == []
    (t,) = res["totals"]
    assert (t["stated"], t["computed"], t["matches"]) == (5000, 1000, False)
    assert "합계" in res["application"]["message"]


def test_total_row_that_matches_is_complete(api, work):
    up = api.upload(HEADER + GOOD + "합계,,,1000,\n")
    work()
    res = api.job(up["job_id"])["result"]
    assert res["application"]["state"] == "applied" and res["totals"][0]["matches"] is True
    assert res["application"]["ack_required"] is False


def test_all_rows_excluded_keeps_previous_version(api, rt, tenant, work):
    v1 = api.upload(HEADER + GOOD)
    work()
    v2 = api.upload(
        HEADER + "가나유통,직매입,PO-1,오류,2025-08-07\n",
        document_id=v1["document"]["document_id"],
        auto_ingest=False,
    )
    # the user confirms the mapping (no cell of the amount column is readable)
    job = api.ingest(
        v2["document"]["id"],
        {
            "format_id": "retail_settlement",
            "columns": {
                "counterparty": 0,
                "trade_type": 1,
                "reference": 2,
                "amount": 3,
                "goods_received_date": 4,
            },
        },
    )
    work()
    app = api.job(job)["result"]["application"]
    assert app["state"] == "not_applied_all_excluded" and app["current_version"] == 1
    assert [r.amount.amount for r in rt.repos.ledger.list(tenant, "settlement_line")] == [1000]


def test_acknowledgment_only_for_the_current_version(api, work):
    v1 = api.upload(HEADER + GOOD + "가나유통,직매입,PO-2,오류,2025-08-07\n")
    work()
    fp = api.job(v1["job_id"])["result"]["application"]["fingerprint"]
    v2 = api.upload(HEADER + GOOD, document_id=v1["document"]["document_id"])
    work()
    assert api.job(v2["job_id"])["result"]["application"]["state"] == "applied"
    r = api.acknowledge(v1["document"]["id"], fp)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_current_version"
    r2 = api.acknowledge(v2["document"]["id"], fp)
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "nothing_to_acknowledge"
