"""End-to-end through HTTP + worker: upload -> ingest -> decisions -> spans -> report."""

from __future__ import annotations

from jt_api_helpers import LEDGER_CSV, agreement_change

API = "/api/v1"


def _upload(client, headers, content: bytes, name: str = "ledger.csv", **form):
    return client.post(
        f"{API}/documents",
        headers=headers,
        files={"file": (name, content, "text/csv")},
        data=form,
    )


def test_health_ready_and_openapi(client):
    assert client.get(f"{API}/health").json()["status"] == "ok"
    r = client.get(f"{API}/ready")
    assert r.status_code == 200, r.text
    assert r.json()["checks"]["migrations"]["current"] == r.json()["checks"]["migrations"]["head"]
    spec = client.get(f"{API}/openapi.json").json()
    for path in (
        "/api/v1/documents",
        "/api/v1/jobs",
        "/api/v1/decisions/{decision_id}",
        "/api/v1/decisions/{decision_id}/approvals",
        "/api/v1/changes",
        "/api/v1/reports/export",
        "/api/v1/document-versions/{doc_version_id}/mapping",
    ):
        assert path in spec["paths"], path
    assert "x-request-id" in {k.lower() for k in client.get(f"{API}/health").headers}


def test_upload_ingest_analysis_detail_report(client, alice, worker, ingest):
    r = _upload(client, alice, LEDGER_CSV.encode(), kind="bank")
    assert r.status_code == 201, r.text
    body = r.json()
    dv = body["document"]["id"]
    assert body["duplicate"] is False and body["job_id"]
    assert worker.run_once() == 1
    job = client.get(f"{API}/jobs/{body['job_id']}", headers=alice).json()
    assert job["status"] == "succeeded", job
    assert job["result"]["document_status"] == "PARSED"
    assert job["result"]["impact"]["changed"] == ["dec:I1"]

    doc = client.get(f"{API}/document-versions/{dv}", headers=alice).json()
    assert doc["status"] == "PARSED"
    spans = client.get(f"{API}/document-versions/{dv}/spans", headers=alice).json()
    assert {s["subject_id"] for s in spans["items"]} == {"I1", "T1"}
    assert spans["items"][0]["locator"] == {"col": "D", "row": 2}
    raw = client.get(f"{API}/document-versions/{dv}/content", headers=alice)
    assert raw.content == LEDGER_CSV.encode()

    lst = client.get(f"{API}/decisions", headers=alice).json()
    assert [d["id"] for d in lst["items"]] == ["dec:I1"]
    det = client.get(f"{API}/decisions/dec:I1", headers=alice).json()
    assert det["status"] == "MATCHED"
    assert det["review_status"] == "VERIFIED"  # citation/number/wording checks passed
    assert {c["name"] for c in det["checks"]} >= {"citation", "numbers", "wording"}
    assert {f["id"] for f in det["facts"]} == {f"f-{dv}-I1", f"f-{dv}-T1"}
    assert det["facts"][0]["span"]["doc_version_id"] == dv
    assert det["variants"] and det["variants"][0]["due_date"] == "2025-10-06"
    assert det["allocations"][0]["amount"] == {"amount": 10_000_000, "currency": "KRW"}
    assert "unresolved" in det and det["unresolved"] == ["rollover"]

    # analysis job (explicit) is deterministic: same result hash
    r = client.post(
        f"{API}/jobs",
        headers=alice,
        json={"type": "run_analysis", "params": {"as_of": "2025-11-01"}},
    )
    assert r.status_code == 202 and r.headers["location"].endswith(r.json()["job_id"])
    worker.run_once()
    again = client.get(f"{API}/decisions/dec:I1", headers=alice).json()
    assert again["result_hash"] == det["result_hash"]

    # correction: an agreement fixes rollover -> decision changes, impact plan in job result
    r = client.post(f"{API}/changes", headers=alice, json={"changes": [agreement_change()]})
    assert r.status_code == 202
    worker.run_once()
    res = client.get(f"{API}/jobs/{r.json()['job_id']}", headers=alice).json()["result"]
    assert res["changed"] == ["dec:I1"] and res["plan"]["fallback_full"] is False
    assert "dec:I1" in res["plan"]["affected"]

    rep = client.post(f"{API}/reports/export", headers=alice, json={"format": "csv"})
    assert rep.status_code == 200
    text = rep.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("decision_id,subject_id,status")
    assert "dec:I1" in text


def test_needs_mapping_then_confirm(client, alice, worker):
    content = "구분,번호,거래처,금액,일자\ninvoice,I9,가나유통,500,2025-08-07\n".encode()
    r = _upload(client, alice, content, name="odd.csv")
    dv = r.json()["document"]["id"]
    worker.run_once()
    doc = client.get(f"{API}/document-versions/{dv}", headers=alice).json()
    assert doc["status"] == "NEEDS_MAPPING"
    m = client.get(f"{API}/document-versions/{dv}/mapping", headers=alice).json()
    assert m["suggestion"]["columns"][0] == "구분" and m["confirmed"] is None
    r = client.post(
        f"{API}/document-versions/{dv}/mapping",
        headers=alice,
        json={
            "mapping": {
                "columns": {"entity": 0, "id": 1, "counterparty": 2, "amount": 3, "date": 4}
            }
        },
    )
    assert r.status_code == 201, r.text
    worker.run_once()
    assert client.get(f"{API}/jobs/{r.json()['job_id']}", headers=alice).json()["status"] == (
        "succeeded"
    )
    assert client.get(f"{API}/document-versions/{dv}", headers=alice).json()["status"] == "PARSED"
    m2 = client.get(f"{API}/document-versions/{dv}/mapping", headers=alice).json()
    assert m2["confirmed"]["mapping"]["columns"]["amount"] == 3
