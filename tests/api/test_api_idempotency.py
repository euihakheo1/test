"""Idempotency-Key on POST create endpoints + content idempotency of uploads."""

from __future__ import annotations

import sqlalchemy as sa
from jt_api_helpers import LEDGER_CSV, invoice_change, txn_change

from jettae.db.orm import ApprovalRow, DocumentVersionRow, JobRow

API = "/api/v1"


def _count(rt, model) -> int:
    with rt.db.session() as s:
        return s.scalar(sa.select(sa.func.count()).select_from(model))


def test_changes_and_jobs_replay(client, alice, rt):
    body = {"changes": [invoice_change(), txn_change()]}
    h = {**alice, "Idempotency-Key": "k-1"}
    r1 = client.post(f"{API}/changes", headers=h, json=body)
    r2 = client.post(f"{API}/changes", headers=h, json=body)
    assert r1.status_code == r2.status_code == 202
    assert r1.json() == r2.json()
    assert r2.headers.get("idempotent-replayed") == "true"
    assert r2.headers["location"] == r1.headers["location"]
    assert _count(rt, JobRow) == 1

    # same key, different request -> 422; other endpoint with the same key is independent
    r3 = client.post(f"{API}/changes", headers=h, json={"changes": [invoice_change("I2")]})
    assert r3.status_code == 422 and r3.json()["error"]["code"] == "idempotency_key_reused"
    r4 = client.post(f"{API}/jobs", headers=h, json={"type": "run_analysis", "params": {}})
    assert r4.status_code == 202
    assert _count(rt, JobRow) == 2

    # without a key every POST creates a new job
    client.post(f"{API}/jobs", headers=alice, json={"type": "run_analysis", "params": {}})
    client.post(f"{API}/jobs", headers=alice, json={"type": "run_analysis", "params": {}})
    assert _count(rt, JobRow) == 4


def test_keys_are_scoped_per_tenant(client, alice, bob, rt):
    h = {"Idempotency-Key": "shared-key"}
    ra = client.post(
        f"{API}/jobs", headers={**alice, **h}, json={"type": "run_analysis", "params": {}}
    )
    rb = client.post(
        f"{API}/jobs", headers={**bob, **h}, json={"type": "run_analysis", "params": {}}
    )
    assert ra.status_code == rb.status_code == 202
    assert ra.json()["job_id"] != rb.json()["job_id"]
    assert "idempotent-replayed" not in rb.headers


def test_upload_replay_and_content_dedupe(client, alice, rt):
    files = {"file": ("ledger.csv", LEDGER_CSV.encode(), "text/csv")}
    h = {**alice, "Idempotency-Key": "up-1"}
    r1 = client.post(f"{API}/documents", headers=h, files=files)
    r2 = client.post(f"{API}/documents", headers=h, files=files)
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json() and r2.headers["idempotent-replayed"] == "true"
    # same bytes without a key: no second version, no second ingest job
    r3 = client.post(f"{API}/documents", headers=alice, files=files)
    assert r3.status_code == 200
    assert r3.json()["duplicate"] is True and r3.json()["job_id"] is None
    assert r3.json()["document"]["id"] == r1.json()["document"]["id"]
    assert _count(rt, DocumentVersionRow) == 1
    assert _count(rt, JobRow) == 1
    # same bytes as a new version of a different document -> 409
    other = client.post(
        f"{API}/documents",
        headers=alice,
        files={"file": ("b.csv", b"entity,id\n", "text/csv")},
    ).json()["document"]["document_id"]
    r4 = client.post(f"{API}/documents", headers=alice, files=files, data={"document_id": other})
    assert r4.status_code == 409 and r4.json()["error"]["code"] == "duplicate_content"
    # a new version of the same document
    r5 = client.post(
        f"{API}/documents",
        headers=alice,
        files={"file": ("ledger.csv", LEDGER_CSV.encode() + b"\n", "text/csv")},
        data={"document_id": r1.json()["document"]["document_id"]},
    )
    assert r5.status_code == 201
    v = r5.json()["document"]
    assert v["version"] == 2 and v["supersedes"] == r1.json()["document"]["id"]
    vs = client.get(f"{API}/documents/{v['document_id']}/versions", headers=alice).json()
    assert [x["version"] for x in vs["items"]] == [1, 2]


def test_approval_replay_creates_one_row(client, alice, worker, rt):
    client.post(f"{API}/changes", headers=alice, json={"changes": [invoice_change(), txn_change()]})
    worker.run_once()
    h = client.get(f"{API}/decisions/dec:I1", headers=alice).json()["result_hash"]
    hk = {**alice, "Idempotency-Key": "appr-1"}
    r1 = client.post(
        f"{API}/decisions/dec:I1/approvals", headers=hk, json={"expected_result_hash": h}
    )
    r2 = client.post(
        f"{API}/decisions/dec:I1/approvals", headers=hk, json={"expected_result_hash": h}
    )
    assert r1.status_code == r2.status_code == 201 and r1.json()["id"] == r2.json()["id"]
    assert _count(rt, ApprovalRow) == 1
    # a failed request does not burn the key
    hk2 = {**alice, "Idempotency-Key": "appr-2"}
    bad = client.post(
        f"{API}/decisions/dec:I1/approvals", headers=hk2, json={"expected_result_hash": "1" * 64}
    )
    assert bad.status_code == 409
    again = client.post(
        f"{API}/decisions/dec:I1/approvals", headers=hk2, json={"expected_result_hash": "1" * 64}
    )
    assert again.status_code == 409 and "idempotent-replayed" not in again.headers
