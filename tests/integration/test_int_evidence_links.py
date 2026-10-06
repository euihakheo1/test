"""F4 through the service: a settlement line and a tax invoice of possibly the same sale.

Real parser -> worker -> SQLite ledger -> analysis -> API. The invoice does not quote the
statement's PO number, so it is never linked automatically (equal amount and date are not a
link): it waits for the user's confirmation, is not counted, and the confirmation is stored
as an ``evidence_link`` ledger record that later full recomputes read back.

The CSV contents are tiny hand-written fixtures (not evaluation data).
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote

import pytest

API = "/api/v1"
APPROVAL = "20250807-41000000-12345678"
HOMETAX = (
    "작성일자,승인번호,공급받는자상호,합계금액,공급가액,세액\n"
    f"2025-08-07,{APPROVAL},(주)가나유통,11000000,10000000,1000000\n"
)
SETTLE_WITH_PO = (
    "거래처,거래형태,발주번호,정산금액,상품수령일\n가나유통,직매입,PO-1001,11000000,2025-08-07\n"
)
BANK = (
    "거래일자,적요,보낸분/받는분,출금액,입금액,잔액\n"
    '2025-10-20,타행입금,가나유통,0,"11,000,000","11,000,000"\n'
)
FILES = [
    (SETTLE_WITH_PO, "settlement", "정산서.csv"),
    (HOMETAX, "tax_invoice", "매출세금계산서.csv"),
    (BANK, "bank", "입금.csv"),
]


def _q(did: str) -> str:
    return quote(did, safe="")


def _decisions(api: Any) -> dict[str, dict[str, Any]]:
    r = api.c.get(f"{API}/decisions", headers=api.h, params={"limit": 200})
    assert r.status_code == 200, r.text
    return {d["id"]: d for d in r.json()["items"]}


def _open_total(api: Any, ids: list[str]) -> int:
    total = 0
    for did in ids:
        d = api.c.get(f"{API}/decisions/{_q(did)}", headers=api.h).json()
        recon = next(c for c in d["computations"] if c["name"] == "recon")
        total += recon["outputs"]["open"]["amount"]
    return total


def _setup(api: Any, work: Any, *, reverse: bool = False) -> dict[str, dict[str, Any]]:
    for text, kind, name in reversed(FILES) if reverse else FILES:
        api.upload(text, kind=kind, name=name)
    work()
    api.analyze()
    work()
    return _decisions(api)


def _pending(decs: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    (pending,) = [d for d in decs.values() if d["counted"] is False]
    (line,) = [d for d in decs.values() if d["basis"] == "settlement_line"]
    return pending, line


@pytest.mark.parametrize("reverse", [False, True])
def test_invoice_waits_for_confirmation_then_is_one_receivable(api, rt, tenant, work, reverse):
    decs = _setup(api, work, reverse=reverse)
    assert len(decs) == 2
    pending, line = _pending(decs)
    assert pending["status"] == "AMBIGUOUS" and "evidence_link" in pending["unresolved"]
    assert pending["basis"] == "invoice" and line["counted"] is True
    detail = api.c.get(f"{API}/decisions/{_q(pending['id'])}", headers=api.h).json()
    assert "확인 대기" in detail["explanation"]
    assert "같은 거래의 정산 행과 세금계산서인지" in detail["explanation"]
    ev = next(c for c in detail["computations"] if c["name"] == "evidence")["outputs"]
    assert ev["confirmation_required"]["candidate_settlement_lines"] == [line["basis_id"]]
    # the pending invoice is not added: open = settlement line only, paid by the bank row
    assert _open_total(api, list(decs)) == 0

    url = f"{API}/decisions/{_q(pending['id'])}/evidence-link"
    stale = api.c.post(
        url,
        headers=api.h,
        json={
            "expected_result_hash": "0" * 64,
            "relation": "same_sale",
            "settlement_line_id": line["basis_id"],
        },
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_result"
    unknown = api.c.post(
        url,
        headers=api.h,
        json={
            "expected_result_hash": pending["result_hash"],
            "relation": "same_sale",
            "settlement_line_id": "stl:nope",
        },
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "unknown_settlement_line"
    bad_body = api.c.post(
        url,
        headers=api.h,
        json={"expected_result_hash": pending["result_hash"], "relation": "maybe"},
    )
    assert bad_body.status_code == 422

    ok = api.c.post(
        url,
        headers=api.h,
        json={
            "expected_result_hash": pending["result_hash"],
            "relation": "same_sale",
            "settlement_line_id": line["basis_id"],
            "confirmed_by": "spoofed",  # unknown field: rejected, the server decides
        },
    )
    assert ok.status_code == 422
    ok = api.c.post(
        url,
        headers=api.h,
        json={
            "expected_result_hash": pending["result_hash"],
            "relation": "same_sale",
            "settlement_line_id": line["basis_id"],
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["link"]["confirmed_by"] == "int@x.example"
    assert pending["id"] in body["removed"]

    after = _decisions(api)
    assert list(after) == [line["id"]]  # one economic receivable
    d = api.c.get(f"{API}/decisions/{_q(line['id'])}", headers=api.h).json()
    ev = next(c for c in d["computations"] if c["name"] == "evidence")["outputs"]
    assert [(x["id"], x["role"], x["method"]) for x in ev["documents"]] == [
        (line["basis_id"], "basis", None),
        (pending["basis_id"], "corroborating", "user_confirmation"),
    ]
    assert d["status"] == "MATCHED" and _open_total(api, list(after)) == 0
    assert "보강 증빙" in d["explanation"] and "사용자 확인" in d["explanation"]

    # stored in the SQL ledger: a full recompute from the database gives the same result
    links = api.c.get(f"{API}/evidence-links", headers=api.h).json()["items"]
    assert [lk["invoice_id"] for lk in links] == [pending["basis_id"]]
    run = rt.service.run_analysis(tenant, as_of=date(2025, 11, 1))
    assert [v.decision.id for v in run.decisions] == [line["id"]]

    # withdrawing the confirmation brings the pending decision back
    w = api.c.delete(f"{API}/evidence-links/{_q(links[0]['id'])}", headers=api.h)
    assert w.status_code == 200, w.text
    back = _decisions(api)
    assert set(back) == {line["id"], pending["id"]}
    assert back[pending["id"]]["counted"] is False


def test_separate_sale_confirmation_counts_the_invoice(api, tenant, work):
    pending, line = _pending(_setup(api, work))
    r = api.c.post(
        f"{API}/decisions/{_q(pending['id'])}/evidence-link",
        headers=api.h,
        json={"expected_result_hash": pending["result_hash"], "relation": "separate_sale"},
    )
    assert r.status_code == 200, r.text
    after = _decisions(api)
    assert set(after) == {pending["id"], line["id"]}
    assert after[pending["id"]]["counted"] is True
    assert "evidence_link" not in after[pending["id"]]["unresolved"]
    # two receivables of 11,000,000 now. The single bank payment of 11,000,000 fits either
    # one, so it is not allocated by guess: both stay open and the allocation is ambiguous.
    assert _open_total(api, list(after)) == 22_000_000
    assert {d["status"] for d in after.values()} == {"AMBIGUOUS"}
    assert all("allocation" in d["unresolved"] for d in after.values())


def test_confirmation_is_tenant_scoped_and_needs_membership(api, client, work):
    from jt_api_helpers import signup

    pending, line = _pending(_setup(api, work))
    other = signup(client, "other@x.example", "다른 회사")
    r = client.post(
        f"{API}/decisions/{_q(pending['id'])}/evidence-link",
        headers=other,
        json={
            "expected_result_hash": pending["result_hash"],
            "relation": "same_sale",
            "settlement_line_id": line["basis_id"],
        },
    )
    assert r.status_code == 404
    assert client.get(f"{API}/evidence-links", headers=other).json()["items"] == []
