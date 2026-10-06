"""Integrator regressions for the API flow (hand-written fixtures only)."""

from __future__ import annotations

import threading
from urllib.parse import quote

import pytest
from jt_api_helpers import LEDGER_CSV, PASSWORD, cookie_header, login_session, signup

from jettae.db.ingest_bridge import ModuleIngest
from jettae.db.runtime import Runtime
from jettae.worker import Worker

API = "/api/v1"

SETTLE_V1 = (
    "정산번호,거래처,거래형태,상품수령일,판매마감일,정산금액\n"
    "A-1,가나유통,직매입,2025-08-07,,10000000\n"
    "A-2,가나유통,직매입,2025-08-08,,3000000\n"
).encode()
# correction: A-2 withdrawn, A-1 unchanged
SETTLE_V2 = (
    "정산번호,거래처,거래형태,상품수령일,판매마감일,정산금액\n"
    "A-1,가나유통,직매입,2025-08-07,,10000000\n"
).encode()
BANK = (
    "거래일자,적요,보낸분/받는분,출금액,입금액,잔액\n"
    '2025-10-20,타행입금,가나유통,0,"10,000,000","10,000,000"\n'
).encode("cp949")


@pytest.fixture
def real(settings, clock, rt):
    r = Runtime.build(settings, clock=clock, ingest=ModuleIngest())
    yield r
    r.close()


def _upload(client, h, content, name, kind, document_id=None, path="/documents"):
    data = {"kind": kind}
    if document_id:
        data["document_id"] = document_id
    return client.post(
        f"{API}{path}", headers=h, files={"file": (name, content, "text/csv")}, data=data
    )


def _settlement_lines(real, client, h):
    tenant = client.get(f"{API}/auth/me", headers=h).json()["tenant_id"]
    return real.repos.ledger.list(tenant, "settlement_line")


def test_correction_upload_replaces_rows_of_superseded_version(real, make_client):
    client = make_client(real)
    h = signup(client, "corr@x.example", "C")
    up = _upload(client, h, SETTLE_V1, "s.csv", "settlement")
    assert up.status_code == 201, up.text
    doc_id = up.json()["document"]["document_id"]
    assert _upload(client, h, BANK, "bank.csv", "bank").status_code == 201
    w = Worker(real, owner="w")
    while w.run_once():
        pass
    lines = _settlement_lines(real, client, h)
    assert sorted(ln.settlement_ref for ln in lines) == ["A-1", "A-2"]
    job = client.post(f"{API}/jobs", headers=h, json={"type": "run_analysis", "params": {}})
    assert job.status_code == 202, job.text
    while w.run_once():
        pass
    a1 = next(ln for ln in lines if ln.settlement_ref == "A-1")
    d = client.get(f"{API}/decisions/{quote('dec:' + a1.id, safe='')}", headers=h).json()
    assert d["status"] == "MATCHED", d
    ok = client.post(
        f"{API}/decisions/{quote('dec:' + a1.id, safe='')}/approvals",
        headers=h,
        json={"expected_result_hash": d["result_hash"]},
    )
    assert ok.status_code == 201, ok.text

    ch = _upload(client, h, SETTLE_V2, "s2.csv", "settlement", doc_id, path="/changes/upload")
    assert ch.status_code == 202, ch.text
    while w.run_once():
        pass
    result = client.get(f"{API}/jobs/{ch.json()['job_id']}", headers=h).json()
    assert result["status"] == "succeeded", result
    lines2 = _settlement_lines(real, client, h)
    # one record per row of the latest version; A-1 keeps its id (UPDATE), A-2 is removed
    assert [ln.settlement_ref for ln in lines2] == ["A-1"]
    assert lines2[0].id == a1.id
    d2 = client.get(f"{API}/decisions/{quote('dec:' + a1.id, safe='')}", headers=h).json()
    assert d2["status"] == "MATCHED"
    assert d2["review_status"] == "REVIEW_REQUIRED"


def test_correction_restoring_earlier_content_creates_new_version(client):
    h = signup(client, "aba@x.example", "R")
    a = LEDGER_CSV.encode()
    b = LEDGER_CSV.replace("10000000,2025-08-07", "9000000,2025-08-07").encode()
    r1 = _upload(client, h, a, "a.csv", "other")
    assert r1.status_code == 201
    doc_id = r1.json()["document"]["document_id"]
    r2 = _upload(client, h, b, "b.csv", "other", doc_id)
    assert r2.status_code == 201 and r2.json()["document"]["version"] == 2
    r3 = _upload(client, h, a, "a.csv", "other", doc_id)
    assert r3.status_code == 201, r3.text
    assert r3.json()["document"]["version"] == 3 and r3.json()["duplicate"] is False
    # re-sending the latest content is still a duplicate
    r4 = _upload(client, h, a, "a.csv", "other", doc_id)
    assert r4.status_code == 200 and r4.json()["duplicate"] is True
    versions = client.get(f"{API}/documents/{doc_id}/versions", headers=h).json()
    assert [v["version"] for v in versions["items"]] == [1, 2, 3]


def _login(client, email):
    return login_session(client, email)


def test_concurrent_refresh_of_one_token_yields_at_most_one_new_token(client, rt, make_client):
    signup(client, "race@x.example", "Race")
    cookies = _login(client, "race@x.example")
    results: list[int] = []
    barrier = threading.Barrier(4)
    # one TestClient per thread: a shared cookie jar would mix the threads' cookies
    clients = [make_client(rt) for _ in range(4)]

    def go(c):
        barrier.wait()
        r = c.post(f"{API}/auth/refresh", headers=cookie_header(cookies))
        results.append(r.status_code)

    ts = [threading.Thread(target=go, args=(c,)) for c in clients]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results.count(200) <= 1
    assert all(code in (200, 401) for code in results)


def test_failed_logins_are_throttled_per_email(client, clock):
    signup(client, "lock@x.example", "L")
    for _ in range(5):
        r = client.post(f"{API}/auth/login", json={"email": "lock@x.example", "password": "nope"})
        assert r.status_code == 401
    r = client.post(f"{API}/auth/login", json={"email": "lock@x.example", "password": PASSWORD})
    assert r.status_code == 429 and r.headers.get("Retry-After")
    clock.advance(901)
    _login(client, "lock@x.example")


def test_nul_in_uploaded_csv_is_reported_as_corrupt(real, make_client):
    client = make_client(real)
    h = signup(client, "nul@x.example", "N")
    rows = ["거래일자,적요,보낸분/받는분,출금액,입금액,잔액"] + [
        f"2025-09-{(i % 28) + 1:02d},입금,가나유통,0,{1000 + i},{1000 + i}" for i in range(400)
    ]
    text = "\n".join(rows)
    content = (text[:9000] + "\x00" + text[9000:]).encode("utf-8")
    up = _upload(client, h, content, "bank.csv", "bank")
    assert up.status_code == 201, up.text
    Worker(real, owner="w").run_once()
    job = client.get(f"{API}/jobs/{up.json()['job_id']}", headers=h).json()
    assert job["status"] == "succeeded", job
    assert job["result"]["document_status"] in ("CORRUPT", "FAILED"), job
    assert job["result"]["records"] == 0


def test_public_due_without_login(client):
    body = {
        "trade_type": "direct",
        "base_date": "2025-08-07",
        "paid_date": "2025-10-20",
        "as_of": None,
        "amount": 10_000_000,
        "rollover": None,
        "rounding": "floor",
    }
    r = client.post(f"{API}/public/due", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["result"] == "due"
    assert out["due_date"] == "2025-10-06"
    assert {v["label"] for v in out["variants"]} <= {"rollover_off", "rollover_on"}
    assert all(isinstance(v["interest"]["amount"], int) for v in out["variants"])
    miss = client.post(f"{API}/public/due", json={**body, "base_date": None})
    assert miss.status_code == 200 and miss.json()["result"] == "insufficient"
    assert miss.json()["required_documents"]
    bad = client.post(f"{API}/public/due", json={**body, "amount": 1.5})
    assert bad.status_code == 422


def test_public_due_is_rate_limited(client):
    body = {"trade_type": "direct", "base_date": "2025-08-07", "amount": 1}
    codes = [client.post(f"{API}/public/due", json=body).status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429
