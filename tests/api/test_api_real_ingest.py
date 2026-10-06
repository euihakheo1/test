"""Integration with the real ingestion package through the bridge (skipped if absent)."""

from __future__ import annotations

import pytest

pytest.importorskip("jettae.ingest.pipeline")

from jettae.db.ingest_bridge import ModuleIngest  # noqa: E402
from jettae.db.runtime import Runtime  # noqa: E402
from jettae.worker import Worker  # noqa: E402

API = "/api/v1"
BANK = (
    "거래일자,적요,보낸분/받는분,출금액,입금액,잔액\n"
    '2025-10-20,타행입금,가나유통,0,"10,000,000","10,000,000"\n'
).encode("cp949")
ODD = "날,돈,메모\n2025-10-20,1000,가나\n".encode()


@pytest.fixture
def real(settings, clock, rt):
    r = Runtime.build(settings, clock=clock, ingest=ModuleIngest())
    yield r
    r.close()


def test_bank_csv_parsed_into_records_and_spans(real, make_client):
    from jt_api_helpers import signup

    client = make_client(real)
    h = signup(client, "ing@x.example", "I")
    up = client.post(
        f"{API}/documents",
        headers=h,
        files={"file": ("bank.csv", BANK, "text/csv")},
        data={"kind": "bank"},
    )
    assert up.status_code == 201, up.text
    Worker(real, owner="w").run_once()
    job = client.get(f"{API}/jobs/{up.json()['job_id']}", headers=h).json()
    assert job["status"] == "succeeded", job
    assert job["result"]["document_status"] == "PARSED" and job["result"]["records"] == 1
    dv = up.json()["document"]["id"]
    spans = client.get(f"{API}/document-versions/{dv}/spans", headers=h).json()["items"]
    assert spans and all(s["excerpt"] for s in spans)
    tenant = client.get(f"{API}/auth/me", headers=h).json()["tenant_id"]
    txns = real.repos.ledger.list(tenant, "bank_txn")
    assert len(txns) == 1 and txns[0].amount.amount == 10_000_000


def test_unknown_layout_needs_mapping_then_confirmed(real, make_client):
    from jt_api_helpers import signup

    client = make_client(real)
    h = signup(client, "map@x.example", "M")
    up = client.post(f"{API}/documents", headers=h, files={"file": ("x.csv", ODD, "text/csv")})
    dv = up.json()["document"]["id"]
    Worker(real, owner="w").run_once()
    doc = client.get(f"{API}/document-versions/{dv}", headers=h).json()
    assert doc["status"] == "NEEDS_MAPPING"
    m = client.get(f"{API}/document-versions/{dv}/mapping", headers=h).json()
    assert m["suggestion"] is not None and m["confirmed"] is None
    r = client.post(
        f"{API}/document-versions/{dv}/mapping",
        headers=h,
        json={"mapping": {"columns": {"txn_date": 0, "deposit": 1, "description": 2}}},
    )
    assert r.status_code == 201
    Worker(real, owner="w").run_once()
    assert client.get(f"{API}/document-versions/{dv}", headers=h).json()["status"] == "PARSED"
