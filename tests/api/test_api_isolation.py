"""Tenant isolation across API, DB repositories, blob store and report export."""

from __future__ import annotations

import jwt
from jt_api_helpers import LEDGER_CSV, invoice_change, txn_change

from jettae.domain.hashing import bytes_hash
from jettae.store.files import FileBlobStore

API = "/api/v1"


def _setup_alice(client, alice, worker):
    up = client.post(
        f"{API}/documents",
        headers=alice,
        files={"file": ("ledger.csv", LEDGER_CSV.encode(), "text/csv")},
        data={"kind": "bank"},
    )
    assert up.status_code == 201, up.text
    worker.run_once()
    det = client.get(f"{API}/decisions/dec:I1", headers=alice).json()
    return up.json(), det


def test_other_tenant_cannot_read_anything(client, alice, bob, worker, rt):
    up, det = _setup_alice(client, alice, worker)
    dv, doc_id, job_id = up["document"]["id"], up["document"]["document_id"], up["job_id"]
    a_tenant = client.get(f"{API}/auth/me", headers=alice).json()["tenant_id"]
    b_tenant = client.get(f"{API}/auth/me", headers=bob).json()["tenant_id"]
    assert a_tenant != b_tenant

    # every read path answers 404 (never 403: existence is not revealed)
    for path in (
        f"{API}/document-versions/{dv}",
        f"{API}/document-versions/{dv}/spans",
        f"{API}/document-versions/{dv}/content",
        f"{API}/document-versions/{dv}/mapping",
        f"{API}/documents/{doc_id}/versions",
        f"{API}/decisions/dec:I1",
        f"{API}/decisions/dec:I1/approvals",
        f"{API}/jobs/{job_id}",
    ):
        r = client.get(path, headers=bob)
        assert r.status_code == 404, (path, r.status_code, r.text)
        assert r.json()["error"]["code"] == "not_found"
    assert client.get(f"{API}/documents", headers=bob).json()["items"] == []
    assert client.get(f"{API}/decisions", headers=bob).json()["items"] == []
    assert client.get(f"{API}/jobs", headers=bob).json()["items"] == []

    # write paths on A's objects
    r = client.post(
        f"{API}/decisions/dec:I1/approvals",
        headers=bob,
        json={"expected_result_hash": det["result_hash"]},
    )
    assert r.status_code == 404
    assert client.post(f"{API}/jobs/{job_id}/cancel", headers=bob).status_code == 404
    r = client.post(
        f"{API}/jobs",
        headers=bob,
        json={"type": "ingest_document", "params": {"doc_version_id": dv}},
    )
    assert r.status_code == 404
    r = client.post(
        f"{API}/document-versions/{dv}/mapping",
        headers=bob,
        json={"mapping": {"columns": {"a": 0}}},
    )
    assert r.status_code == 404
    r = client.post(
        f"{API}/document-versions/{dv}/acknowledge", headers=bob, json={"fingerprint": "0" * 64}
    )
    assert r.status_code == 404

    # report export: explicit ids of A -> 404; "all" for B -> empty report
    r = client.post(
        f"{API}/reports/export", headers=bob, json={"decision_ids": ["dec:I1"], "format": "csv"}
    )
    assert r.status_code == 404
    r = client.post(f"{API}/reports/export", headers=bob, json={"format": "html"})
    assert r.status_code == 200 and b"dec:I1" not in r.content

    # A still sees everything
    assert client.get(f"{API}/decisions/dec:I1", headers=alice).status_code == 200

    # DB repositories are tenant-scoped
    assert rt.repos.decisions.current(b_tenant) == {}
    assert rt.repos.documents.get(b_tenant, dv) is None
    assert rt.repos.facts.list(b_tenant) == []
    assert rt.repos.ledger.list(b_tenant, "invoice") == []
    assert rt.repos.approvals.list_for(b_tenant, "dec:I1") == []
    assert rt.repos.results.load(b_tenant) is None
    assert rt.queue.get(b_tenant, job_id) is None


def test_blob_store_never_crosses_tenants(client, alice, bob, worker, rt, tmp_path):
    up, _ = _setup_alice(client, alice, worker)
    a_tenant = client.get(f"{API}/auth/me", headers=alice).json()["tenant_id"]
    b_tenant = client.get(f"{API}/auth/me", headers=bob).json()["tenant_id"]
    key = bytes_hash(LEDGER_CSV.encode())
    assert rt.blobs.get(a_tenant, key) == LEDGER_CSV.encode()
    assert rt.blobs.get(b_tenant, key) is None  # same hash, other tenant: not visible
    assert not rt.blobs.exists(b_tenant, key)
    # B uploads identical bytes: stored separately, B's own document (no cross-tenant dedupe)
    r = client.post(
        f"{API}/documents",
        headers=bob,
        files={"file": ("same.csv", LEDGER_CSV.encode(), "text/csv")},
        data={"auto_ingest": "false"},
    )
    assert r.status_code == 201 and r.json()["duplicate"] is False
    assert r.json()["document"]["id"] != up["document"]["id"]
    assert rt.blobs.exists(b_tenant, key)
    assert rt.blobs._path(a_tenant, key) != rt.blobs._path(b_tenant, key)
    # key validation: no traversal
    store = FileBlobStore(tmp_path / "s")
    assert store.get(a_tenant, "../" + key) is None
    assert store.get(a_tenant, key.upper()) is None


def test_tenant_comes_from_credentials_only(client, alice, bob, worker, rt):
    a_tenant = client.get(f"{API}/auth/me", headers=alice).json()["tenant_id"]
    b_tenant = client.get(f"{API}/auth/me", headers=bob).json()["tenant_id"]
    # Bob sends records claiming Alice's tenant (body) and a tenant header: both ignored
    ch = invoice_change("X1", 5_000)
    ch["record"]["tenant_id"] = a_tenant
    r = client.post(
        f"{API}/changes",
        headers={**bob, "X-Tenant-ID": a_tenant},
        json={"changes": [ch, txn_change("TX1", 5_000)]},
    )
    assert r.status_code == 202, r.text
    worker.run_once()
    assert rt.repos.ledger.get(b_tenant, "invoice", "X1") is not None
    assert rt.repos.ledger.get(a_tenant, "invoice", "X1") is None
    assert client.get(f"{API}/decisions/dec:X1", headers=alice).status_code == 404

    # a token re-signed with another tenant id but a wrong key is rejected
    token = alice["Authorization"].split()[1]
    claims = jwt.decode(token, options={"verify_signature": False})
    claims["tid"] = b_tenant
    forged = jwt.encode(claims, "not-the-server-secret-" + "y" * 30, algorithm="HS256")
    r = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    # alg=none tokens are rejected
    none_tok = jwt.encode(claims, None, algorithm="none")
    r = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {none_tok}"})
    assert r.status_code == 401
    # a validly signed token for a tenant the user is not a member of is rejected
    signed = jwt.encode(claims, rt.settings.jwt_secret, algorithm="HS256")
    r = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {signed}"})
    assert r.status_code == 401
    assert client.get(f"{API}/decisions", headers={}).status_code == 401
