"""Second external review (2026-10-06): regressions through the REAL parser -> worker ->
SQLite -> API path.

* unread sheet in a correction must not delete that sheet's rows (pipeline + doc_apply)
* a column mapping applies to its own table only (pipeline / MappingRequest.table)
* an unreadable cell in a correction keeps the earlier receivable (carry-over) until the
  user acknowledges the version
* a confirmed mapping is reused for the next version of the same document (worker)

Every file here is a tiny hand-written fixture (not evaluation data).
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from openpyxl import Workbook

from jettae.app.contracts import MappingContractError
from jettae.ingest.pipeline import parse_document

STD = ["거래처", "거래형태", "발주번호", "정산금액", "상품수령일"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx(sheets: dict[str, list[list[Any]]]) -> bytes:
    wb = Workbook()
    assert wb.active is not None
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def upload_xlsx(api: Any, content: bytes, document_id: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"kind": "settlement", "auto_ingest": "true"}
    if document_id:
        data["document_id"] = document_id
    r = api.c.post(
        "/api/v1/documents",
        headers=api.h,
        files={"file": ("정산.xlsx", content, XLSX)},
        data=data,
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


def amounts(rt: Any, tenant: str) -> list[int]:
    return sorted(r.amount.amount for r in rt.repos.ledger.list(tenant, "settlement_line"))


def row(ref: str, amount: Any) -> list[Any]:
    return ["가나유통", "직매입", ref, amount, "2025-08-07"]


# --------------------------------------------------------------------------- finding 1
def test_unrecognised_sheet_in_a_correction_keeps_that_sheets_rows(api, rt, tenant, work):
    v1 = upload_xlsx(
        api,
        xlsx({"8월": [STD, row("PO-1", 1000)], "9월": [STD, row("PO-2", 5000), row("PO-3", 7000)]}),
    )
    work()
    assert api.job(v1["job_id"])["result"]["application"]["state"] == "applied"
    assert amounts(rt, tenant) == [1000, 5000, 7000]

    bad_header = ["Vendor", "Type", "PO", "Amt", "Recv"]
    v2 = upload_xlsx(
        api,
        xlsx(
            {
                "8월": [STD, row("PO-1", 1000)],
                "9월": [bad_header, row("PO-2", 5000), row("PO-3", 7000)],
            }
        ),
        v1["document"]["document_id"],
    )
    work()
    res = api.job(v2["job_id"])["result"]
    app = res["application"]
    assert res["document_status"] == "PARSED"
    # not "every row applied": the unread sheet is counted and blocks approval
    assert app["state"] == "applied_needs_ack" and app["ack_required"] is True
    assert res["counts"]["tables_unread"] == 1 and res["counts"]["unread_rows"] == 2
    assert "모두 반영" not in app["message"] and "읽지 않았습니다" in app["message"]
    # nothing removed: the two 9월 rows are carried over from v1
    assert app["records_removed"] == 0 and app["records_carried_over"] == 2
    assert amounts(rt, tenant) == [1000, 5000, 7000]

    # the carried-over rows block approval of their decisions until v2 is acknowledged
    api.analyze()
    work()
    po2 = next(
        ln for ln in rt.repos.ledger.list(tenant, "settlement_line") if ln.reference == "PO-2"
    )
    d = api.decision(po2.id)
    r = api.approve(po2.id, d["result_hash"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "document_ack_required"
    assert r.json()["error"]["details"]["documents"][0]["doc_version_id"] == v2["document"]["id"]

    # acknowledging v2 = "this version is complete as read": the carried rows go now
    ack = api.acknowledge(v2["document"]["id"], app["fingerprint"])
    assert ack.status_code == 200, ack.text
    assert ack.json()["records_removed"] == 2 and len(ack.json()["removed"]) == 2
    assert amounts(rt, tenant) == [1000]


def test_unrecognised_sheet_on_first_upload_is_reported(api, rt, tenant, work):
    up = upload_xlsx(
        api,
        xlsx(
            {
                "8월": [STD, row("PO-1", 1000)],
                "9월": [["Vendor", "Type", "PO", "Amt", "Recv"], row("PO-2", 5000)],
            }
        ),
    )
    work()
    res = api.job(up["job_id"])["result"]
    assert res["application"]["state"] == "applied_needs_ack"
    assert res["counts"]["tables_unread"] == 1 and res["counts"]["unread_rows"] == 1
    assert any("9월" in n for n in res["notes"])
    assert amounts(rt, tenant) == [1000]


def test_zero_rows_plus_unread_sheet_is_not_an_empty_revision(api, rt, tenant, work):
    v1 = upload_xlsx(api, xlsx({"8월": [STD, row("PO-1", 1000)]}))
    work()
    v2 = upload_xlsx(
        api,
        xlsx({"8월": [STD], "9월": [["Vendor", "Type", "PO", "Amt", "Recv"], row("PO-1", 1000)]}),
        v1["document"]["document_id"],
    )
    work()
    app = api.job(v2["job_id"])["result"]["application"]
    assert app["state"] == "not_applied_empty_unverified"
    assert amounts(rt, tenant) == [1000]


# --------------------------------------------------------------------------- finding 2
SHEET_A = [
    ["번호", "금액", "업체", "형태", "일자"],
    [9001, 1000, "가나유통", "직매입", "2025-08-07"],
]
SHEET_B = [
    ["정산금액", "발주번호", "거래처", "거래형태", "상품수령일"],
    [3000, 4444, "가나유통", "직매입", "2025-08-07"],
]
A_COLUMNS = {
    "reference": 0,
    "amount": 1,
    "counterparty": 2,
    "trade_type": 3,
    "goods_received_date": 4,
}


def _parse(content: bytes, mapping: dict[str, Any] | None) -> Any:
    return parse_document(
        content,
        filename="two.xlsx",
        media_type=XLSX,
        kind="settlement",
        tenant_id="t",
        doc_version_id="v1",
        document_key="d1",
        mapping=mapping,
    )


def _by_sheet(res: Any) -> dict[str, tuple[str | None, int]]:
    out = {}
    for rec in res.records:
        sheet = next(
            f.span.locator["sheet"]
            for f in res.facts
            if f.subject_id == rec.id and f.span is not None
        )
        out[sheet] = (rec.reference, rec.amount.amount)
    return out


# a sheet no format recognises (column order differs from sheet B)
SHEET_C = [["코드", "값", "회사", "구분", "날"], [9001, 1000, "가나유통", "직매입", "2025-08-07"]]
SHEET_D = [
    ["Vendor", "Type", "PO", "Amt", "Recv"],
    ["가나유통", "직매입", "PO-9", 50, "2025-08-07"],
]


def test_mapping_for_one_sheet_never_rewrites_another_sheet():
    # A and B are both recognised automatically, with different column orders
    content = xlsx({"A": SHEET_A, "B": SHEET_B})
    auto = _parse(content, None)
    assert _by_sheet(auto) == {"A": ("9001", 1000), "B": ("4444", 3000)}
    # the user's mapping for sheet A without a table name cannot be attributed: rejected
    with pytest.raises(MappingContractError, match="name the table"):
        _parse(content, {"format_id": "retail_settlement", "columns": A_COLUMNS})
    # with the table named (what the UI sends) it applies to A only; B keeps its own columns
    named = _parse(content, {"format_id": "retail_settlement", "table": "A", "columns": A_COLUMNS})
    assert named.status.value == "PARSED" and not named.issues, named
    assert _by_sheet(named) == {"A": ("9001", 1000), "B": ("4444", 3000)}

    # an unnamed mapping on a file with ONE unrecognised sheet applies to that sheet only,
    # never to the sheet recognised with confirmed confidence
    mixed = xlsx({"C": SHEET_C, "B": SHEET_B})
    unnamed = _parse(mixed, {"format_id": "retail_settlement", "columns": A_COLUMNS})
    assert _by_sheet(unnamed) == {"C": ("9001", 1000), "B": ("4444", 3000)}
    assert unnamed.counts is not None and unnamed.counts.tables_unread == 0


def test_mapping_table_must_exist_and_unnamed_mapping_must_be_unambiguous():
    content = xlsx({"A": SHEET_A, "B": SHEET_B})
    with pytest.raises(MappingContractError, match="table"):
        _parse(content, {"table": "없는시트", "columns": A_COLUMNS})
    # two unrecognised sheets with different header rows: an unnamed mapping is ambiguous
    with pytest.raises(MappingContractError, match="name the table"):
        _parse(xlsx({"C": SHEET_C, "D": SHEET_D}), {"columns": A_COLUMNS})


def test_mapping_with_table_through_worker(api, rt, tenant, work):
    up = upload_xlsx(api, xlsx({"C": SHEET_C, "B": SHEET_B}))
    work()
    res = api.job(up["job_id"])["result"]
    assert res["document_status"] == "PARSED"
    assert res["application"]["state"] == "applied_needs_ack"  # sheet C was not read
    assert res["counts"]["tables_unread"] == 1
    dvid = up["document"]["id"]
    m = api.c.get(f"/api/v1/document-versions/{dvid}/mapping", headers=api.h).json()
    assert m["suggestion"]["table"] == "C"  # the UI sends this table name back
    r = api.confirm_mapping(
        dvid, {"format_id": "retail_settlement", "table": "C", "columns": A_COLUMNS}
    )
    assert r.status_code == 201, r.text
    work()
    job = api.job(r.json()["job_id"])
    assert job["status"] == "succeeded", job
    assert job["result"]["mapping"]["kind"] == "job"
    assert job["result"]["application"]["state"] == "applied"
    lines = {
        ln.reference: ln.amount.amount for ln in rt.repos.ledger.list(tenant, "settlement_line")
    }
    assert lines == {"9001": 1000, "4444": 3000}


# --------------------------------------------------------------------------- finding 3
HEADER = ",".join(STD) + "\n"
BANK = (
    '거래일자,적요,보낸분/받는분,출금액,입금액,잔액\n2025-10-20,PO-2,가나유통,0,"2,000","2,000"\n'
)
DOC_B = (
    "가나유통,직매입,PO-2,2000,2025-08-07\n"
    "가나유통,직매입,PO-3,10,2025-08-07\n"
    "가나유통,직매입,PO-4,20,2025-08-07\n"
    "가나유통,직매입,PO-5,30,2025-08-07\n"
)


def _line(rt: Any, tenant: str, ref: str) -> Any:
    return next(
        (ln for ln in rt.repos.ledger.list(tenant, "settlement_line") if ln.reference == ref), None
    )


def test_unreadable_cell_in_a_correction_keeps_the_receivable(api, rt, tenant, work):
    a = api.upload(HEADER + "나다상사,직매입,PO-1,1000,2025-08-07\n", name="a.csv")
    b1 = api.upload(HEADER + DOC_B, name="b.csv")
    api.upload(BANK, kind="bank", name="bank.csv")
    work()
    api.analyze()
    work()
    po2 = _line(rt, tenant, "PO-2")
    d = api.decision(po2.id)
    assert d["status"] == "MATCHED"

    b2 = api.upload(
        HEADER + DOC_B.replace("PO-2,2000", "PO-2,금액오류"),
        document_id=b1["document"]["document_id"],
        name="b.csv",
    )
    work()
    app = api.job(b2["job_id"])["result"]["application"]
    assert app["state"] == "applied_needs_ack"
    assert app["records_removed"] == 0 and app["records_carried_over"] == 1
    assert app["carried_over"] == [f"settlement_line:{po2.id}"]
    # the receivable, its decision and its payment allocation are still there
    assert _line(rt, tenant, "PO-2") is not None
    api.analyze()
    work()
    d2 = api.decision(po2.id)
    assert d2["status"] == "MATCHED"
    recon = next(c for c in d2["computations"] if c["name"] == "recon")
    assert recon["outputs"]["allocated"]["amount"] == 2000
    # its approval waits for the acknowledgment of v2 (it cites the carried-over v1 row)
    r = api.approve(po2.id, d2["result_hash"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "document_ack_required"
    # an unrelated document is not blocked
    po1 = _line(rt, tenant, "PO-1")
    d1 = api.decision(po1.id)
    assert api.approve(po1.id, d1["result_hash"]).status_code == 201
    assert a  # (document A uploaded first)

    # acknowledgment removes the carried-over row (explicit user decision, reported)
    ack = api.acknowledge(b2["document"]["id"], app["fingerprint"])
    assert ack.status_code == 200, ack.text
    assert ack.json()["removed"] == [f"settlement_line:{po2.id}"]
    assert _line(rt, tenant, "PO-2") is None


def test_complete_correction_still_removes_missing_rows(api, rt, tenant, work):
    b1 = api.upload(HEADER + DOC_B, name="b.csv")
    work()
    b2 = api.upload(
        HEADER + DOC_B.replace("가나유통,직매입,PO-2,2000,2025-08-07\n", ""),
        document_id=b1["document"]["document_id"],
        name="b.csv",
    )
    work()
    app = api.job(b2["job_id"])["result"]["application"]
    assert app["state"] == "applied"
    assert app["records_removed"] == 1 and app["records_carried_over"] == 0
    assert _line(rt, tenant, "PO-2") is None


# --------------------------------------------------------------------------- finding 6
def test_confirmed_mapping_is_reused_for_the_next_version(api, rt, tenant, work):
    header = "거래처,거래형태,발주번호,정산금액,입고일\n"
    v1 = api.upload(header + "가나유통,직매입,PO-1,1000,2025-08-07\n")
    work()
    # 입고일 is not the statutory 상품수령일: the user removes it as base date
    r = api.confirm_mapping(v1["document"]["id"], {"columns": {"goods_received_date": None}})
    assert r.status_code == 201, r.text
    work()
    (ln,) = rt.repos.ledger.list(tenant, "settlement_line")
    assert (ln.amount.amount, ln.goods_received_date) == (1000, None)

    v2 = api.upload(
        header + "가나유통,직매입,PO-1,1200,2025-08-07\n", document_id=v1["document"]["document_id"]
    )
    work()
    res = api.job(v2["job_id"])["result"]
    assert res["application"]["state"] == "applied"
    assert res["mapping"]["kind"] == "inherited"
    assert res["mapping"]["doc_version_id"] == v1["document"]["id"]
    (ln,) = rt.repos.ledger.list(tenant, "settlement_line")
    assert (ln.amount.amount, ln.goods_received_date) == (1200, None)


def test_inherited_mapping_follows_reordered_columns(api, rt, tenant, work):
    v1 = api.upload(
        "메모,거래처,거래형태,발주번호,정산금액,상품수령일\n정산,가나유통,직매입,PO-1,1000,2025-08-07\n"
    )
    work()
    cols = {
        "counterparty": 1,
        "trade_type": 2,
        "reference": 3,
        "amount": 4,
        "goods_received_date": 5,
    }
    r = api.confirm_mapping(
        v1["document"]["id"], {"format_id": "retail_settlement", "columns": cols}
    )
    assert r.status_code == 201, r.text
    work()
    # v2 moves the memo column to the end: indexes change, header texts do not
    v2 = api.upload(
        "거래처,거래형태,발주번호,정산금액,상품수령일,메모\n가나유통,직매입,PO-1,1500,2025-08-07,정산\n",
        document_id=v1["document"]["document_id"],
    )
    work()
    res = api.job(v2["job_id"])["result"]
    assert res["mapping"]["kind"] == "inherited", res
    (ln,) = rt.repos.ledger.list(tenant, "settlement_line")
    assert (ln.counterparty, ln.reference, ln.amount.amount) == ("가나유통", "PO-1", 1500)


def test_mapping_that_no_longer_fits_requires_a_new_mapping(api, rt, tenant, work):
    header = "거래처,거래형태,발주번호,정산금액,입고일\n"
    v1 = api.upload(header + "가나유통,직매입,PO-1,1000,2025-08-07\n")
    work()
    api.confirm_mapping(v1["document"]["id"], {"columns": {"goods_received_date": None}})
    work()
    # the header row changed: whether the user would remove the new column is unknown
    v2 = api.upload(
        "거래처,거래형태,발주번호,정산금액,상품수령일\n가나유통,직매입,PO-1,1200,2025-08-07\n",
        document_id=v1["document"]["document_id"],
    )
    work()
    res = api.job(v2["job_id"])["result"]
    assert res["document_status"] == "NEEDS_MAPPING"
    assert res["application"]["state"] == "not_applied_parse"
    assert res["mapping"]["kind"] == "none" and "v1" in res["mapping"]["not_inherited"]
    (ln,) = rt.repos.ledger.list(tenant, "settlement_line")
    assert (ln.amount.amount, ln.goods_received_date) == (1000, None)  # v1 kept
