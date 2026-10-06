"""Second external review (2026-10-06), evidence layer, through the REAL parser -> worker ->
SQLite -> analysis -> API path:

* a tax invoice whose direction/counterparty is unknown is never counted as a receivable
* the same settlement line uploaded in two documents is counted once until confirmed

Every file here is a tiny hand-written fixture (not evaluation data).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

API = "/api/v1"
SETTLE = "거래처,거래형태,발주번호,정산금액,상품수령일\n가나유통,직매입,PO-1,1100,2025-08-07\n"
HOMETAX_NO_DIRECTION = (
    "작성일자,승인번호,공급받는자상호,합계금액,공급가액,세액\n"
    "2025-08-07,20250807-1,(주)가나유통,1100,1000,100\n"
)


def _q(did: str) -> str:
    return quote(did, safe="")


def _decisions(api: Any) -> dict[str, dict[str, Any]]:
    r = api.c.get(f"{API}/decisions", headers=api.h, params={"limit": 200})
    assert r.status_code == 200, r.text
    return {d["id"]: d for d in r.json()["items"]}


def _detail(api: Any, did: str) -> dict[str, Any]:
    r = api.c.get(f"{API}/decisions/{_q(did)}", headers=api.h)
    assert r.status_code == 200, r.text
    return dict(r.json())


def _open(detail: dict[str, Any]) -> int:
    recon = next(c for c in detail["computations"] if c["name"] == "recon")
    return int(recon["outputs"]["open"]["amount"])


def _open_total(api: Any) -> int:
    return sum(_open(_detail(api, did)) for did in _decisions(api))


def _analyze(api: Any, work: Any) -> dict[str, dict[str, Any]]:
    api.analyze()
    work()
    return _decisions(api)


# --------------------------------------------------------------------------- finding 4
def test_invoice_of_unknown_direction_is_not_a_receivable(api, rt, tenant, work):
    api.upload(SETTLE, name="정산서.csv")
    inv_up = api.upload(HOMETAX_NO_DIRECTION, kind="tax_invoice", name="hometax.csv")
    work()
    res = api.job(inv_up["job_id"])["result"]
    # the unknown direction is reported and makes the version partial
    assert res["application"]["state"] == "applied_needs_ack"
    assert any(i["field"] == "direction" and i["kind"] == "value" for i in res["issues"])

    decs = _analyze(api, work)
    assert len(decs) == 2
    (inv,) = [d for d in decs.values() if d["basis"] == "invoice"]
    (line,) = [d for d in decs.values() if d["basis"] == "settlement_line"]
    assert inv["counted"] is False and line["counted"] is True
    d = _detail(api, inv["id"])
    assert d["status"] == "INSUFFICIENT_EVIDENCE"
    assert "invoice_direction" in d["unresolved"]
    assert {"direction", "counterparty"} <= set(d["missing"])
    assert any("매출" in doc for doc in d["required_documents"])
    assert _open(d) == 0
    ev = next(c for c in d["computations"] if c["name"] == "evidence")["outputs"]
    assert ev["confirmation_required"]["kind"] == "invoice_direction"
    assert ev["confirmation_required"]["choices"] == []
    # one 1,100 sale: the counted open total is 1,100, not 2,200
    assert _open_total(api) == 1100

    # no same/separate-sale answer can make it a receivable
    r = api.c.post(
        f"{API}/decisions/{_q(inv['id'])}/evidence-link",
        headers=api.h,
        json={"expected_result_hash": d["result_hash"], "relation": "separate_sale"},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "direction_unknown"

    # re-read with the direction: the invoice gets its counterparty and waits for the
    # usual link confirmation (its approval number is not the statement's PO number)
    r = api.confirm_mapping(inv_up["document"]["id"], {"options": {"direction": "sales"}})
    assert r.status_code == 201, r.text
    work()
    assert api.job(r.json()["job_id"])["result"]["application"]["state"] == "applied"
    decs = _analyze(api, work)
    (inv,) = [d for d in decs.values() if d["basis"] == "invoice"]
    d = _detail(api, inv["id"])
    assert d["status"] == "AMBIGUOUS" and "evidence_link" in d["unresolved"]
    assert "direction" not in d["missing"]
    assert _open_total(api) == 1100


# --------------------------------------------------------------------------- finding 5
LINE = "거래처,거래형태,발주번호,정산금액,상품수령일\n가나유통,직매입,PO-1,1000,2025-08-07\n"
BANK = (
    "거래일자,적요,보낸분/받는분,출금액,입금액,잔액\n"
    '2025-10-20,타행입금,가나유통,0,"1,000","1,000"\n'
)


def _two_copies(api: Any, work: Any, *, bank: bool) -> dict[str, dict[str, Any]]:
    api.upload(LINE, name="a.csv")
    api.upload("﻿" + LINE, name="a_reissued.csv")  # same rows, different bytes
    if bank:
        api.upload(BANK, kind="bank", name="bank.csv")
    work()
    return _analyze(api, work)


def test_same_line_in_two_documents_is_counted_once(api, rt, tenant, work):
    decs = _two_copies(api, work, bank=False)
    assert len(rt.repos.ledger.list(tenant, "settlement_line")) == 2  # two document rows
    counted = [d for d in decs.values() if d["counted"]]
    pending = [d for d in decs.values() if not d["counted"]]
    assert len(counted) == 1 and len(pending) == 1
    p = _detail(api, pending[0]["id"])
    assert p["status"] == "AMBIGUOUS" and "duplicate_line" in p["unresolved"]
    ev = next(c for c in p["computations"] if c["name"] == "evidence")["outputs"]
    assert ev["confirmation_required"]["kind"] == "duplicate_line"
    assert ev["confirmation_required"]["candidate_settlement_lines"] == [counted[0]["basis_id"]]
    assert _open_total(api) == 1000


def test_duplicate_line_with_payment_then_confirmed_separate(api, rt, tenant, work):
    decs = _two_copies(api, work, bank=True)
    (counted,) = [d for d in decs.values() if d["counted"]]
    (pending,) = [d for d in decs.values() if not d["counted"]]
    assert counted["status"] == "MATCHED"  # the one payment is not split over two copies
    assert _open_total(api) == 0
    r = api.c.post(
        f"{API}/decisions/{_q(pending['id'])}/evidence-link",
        headers=api.h,
        json={"expected_result_hash": pending["result_hash"], "relation": "separate_sale"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["link"]["document_entity"] == "settlement_line"
    decs = _decisions(api)
    assert all(d["counted"] for d in decs.values()) and len(decs) == 2
    # two confirmed sales of 1,000 and one payment of 1,000 without a reference: the
    # payment is not guessed (both AMBIGUOUS, nothing allocated), as for any equal amounts
    assert {d["status"] for d in decs.values()} == {"AMBIGUOUS"}
    assert _open_total(api) == 2000


def test_duplicate_line_confirmed_same_is_corroborating(api, rt, tenant, work):
    decs = _two_copies(api, work, bank=False)
    (counted,) = [d for d in decs.values() if d["counted"]]
    (pending,) = [d for d in decs.values() if not d["counted"]]
    url = f"{API}/decisions/{_q(pending['id'])}/evidence-link"
    bad = api.c.post(
        url,
        headers=api.h,
        json={
            "expected_result_hash": pending["result_hash"],
            "relation": "same_sale",
            "settlement_line_id": "stl:nope",
        },
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "unknown_settlement_line"
    r = api.c.post(
        url,
        headers=api.h,
        json={
            "expected_result_hash": pending["result_hash"],
            "relation": "same_sale",
            "settlement_line_id": counted["basis_id"],
        },
    )
    assert r.status_code == 200, r.text
    decs = _decisions(api)
    assert list(decs) == [counted["id"]]
    d = _detail(api, counted["id"])
    docs = next(c for c in d["computations"] if c["name"] == "evidence")["outputs"]["documents"]
    assert [(x["id"], x["role"], x["method"]) for x in docs[1:]] == [
        (pending["basis_id"], "corroborating", "user_confirmation")
    ]
    assert _open_total(api) == 1000
    # withdrawing the confirmation brings the pending duplicate back
    (link,) = api.c.get(f"{API}/evidence-links", headers=api.h).json()["items"]
    assert api.c.delete(f"{API}/evidence-links/{_q(link['id'])}", headers=api.h).status_code == 200
    assert len(_decisions(api)) == 2 and _open_total(api) == 1000


def test_full_recompute_equals_incremental_for_duplicates(api, rt, tenant, work):
    from datetime import date

    from jettae.evidence.engine import full_recompute
    from jettae.evidence.snapshot import AnalysisConfig

    _two_copies(api, work, bank=True)
    snap = rt.service.snapshot(tenant, AnalysisConfig(as_of=date(2025, 11, 1)))
    full = full_recompute(snap)
    stored = {k: d.result_hash for k, d in rt.repos.decisions.current(tenant).items()}
    assert {k: v.result_hash for k, v in full.decisions.items()} == stored
