"""Approvals: optimistic concurrency (409), history kept, validity re-checked at export."""

from __future__ import annotations

import csv
import io

from jt_api_helpers import agreement_change, invoice_change, txn_change

API = "/api/v1"


def _analysed(client, headers, worker):
    r = client.post(
        f"{API}/changes", headers=headers, json={"changes": [invoice_change(), txn_change()]}
    )
    assert r.status_code == 202, r.text
    worker.run_once()
    return client.get(f"{API}/decisions/dec:I1", headers=headers).json()


def test_approve_conflict_and_review_required(client, alice, worker):
    det = _analysed(client, alice, worker)
    h1 = det["result_hash"]

    r = client.post(
        f"{API}/decisions/dec:I1/approvals", headers=alice, json={"expected_result_hash": "0" * 64}
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "stale_result" and err["details"]["current_result_hash"] == h1

    r = client.post(
        f"{API}/decisions/dec:I1/approvals", headers=alice, json={"expected_result_hash": h1}
    )
    assert r.status_code == 201, r.text
    a1 = r.json()
    assert a1["current"] is True and a1["approved_by"] == "alice@a.example"
    assert client.get(f"{API}/decisions/dec:I1", headers=alice).json()["review_status"] == (
        "APPROVED"
    )
    ok = client.post(
        f"{API}/reports/export",
        headers=alice,
        json={"decision_ids": ["dec:I1"], "require_approved": True, "format": "csv"},
    )
    assert ok.status_code == 200

    # new evidence (agreement) changes the result: approval kept, status REVIEW_REQUIRED
    r = client.post(f"{API}/changes", headers=alice, json={"changes": [agreement_change()]})
    worker.run_once()
    job = client.get(f"{API}/jobs/{r.json()['job_id']}", headers=alice).json()
    assert job["result"]["review_required"] == ["dec:I1"]
    det2 = client.get(f"{API}/decisions/dec:I1", headers=alice).json()
    assert det2["review_status"] == "REVIEW_REQUIRED"
    assert det2["result_hash"] != h1
    assert [a["id"] for a in det2["approvals"]] == [a1["id"]]
    assert det2["approvals"][0]["current"] is False
    assert [h["result_hash"] for h in det2["history"]] == [h1, det2["result_hash"]]

    # approving with the old hash is a conflict; the report re-check refuses as well
    r = client.post(
        f"{API}/decisions/dec:I1/approvals", headers=alice, json={"expected_result_hash": h1}
    )
    assert r.status_code == 409
    r = client.post(
        f"{API}/reports/export",
        headers=alice,
        json={"decision_ids": ["dec:I1"], "require_approved": True},
    )
    assert r.status_code == 409
    assert r.json()["error"]["details"]["invalid"] == {"dec:I1": "REVIEW_REQUIRED"}
    # without require_approved the report is produced but says the item is not valid now
    r = client.post(f"{API}/reports/export", headers=alice, json={"format": "csv"})
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    cell = dict(zip(rows[0], rows[1], strict=True))
    assert cell["review_status"] == "REVIEW_REQUIRED" and cell["valid_at_export"] == "no"

    r = client.post(
        f"{API}/decisions/dec:I1/approvals",
        headers=alice,
        json={"expected_result_hash": det2["result_hash"]},
    )
    assert r.status_code == 201
    lst = client.get(f"{API}/decisions/dec:I1/approvals", headers=alice).json()["items"]
    assert [a["current"] for a in lst] == [False, True]


def test_bad_hash_format_is_422_and_unknown_decision_404(client, alice, worker):
    _analysed(client, alice, worker)
    r = client.post(
        f"{API}/decisions/dec:I1/approvals", headers=alice, json={"expected_result_hash": "abc"}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = client.post(
        f"{API}/decisions/dec:NOPE/approvals",
        headers=alice,
        json={"expected_result_hash": "0" * 64},
    )
    assert r.status_code == 404


def test_filters_and_cursor_pagination(client, alice, worker):
    changes = [invoice_change(f"I{n}", 1_000 * n) for n in range(1, 6)]
    changes[2]["record"]["goods_received_date"] = None  # -> INSUFFICIENT_EVIDENCE
    r = client.post(f"{API}/changes", headers=alice, json={"changes": changes})
    assert r.status_code == 202, r.text
    worker.run_once()
    seen, cursor = [], None
    while True:
        q = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get(f"{API}/decisions", headers=alice, params=q).json()
        seen += [d["id"] for d in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [f"dec:I{n}" for n in range(1, 6)]
    ins = client.get(
        f"{API}/decisions", headers=alice, params={"status": "INSUFFICIENT_EVIDENCE"}
    ).json()["items"]
    assert [d["id"] for d in ins] == ["dec:I3"]
    assert ins[0]["missing"] == ["goods_received_date"] and ins[0]["required_documents"]
    rv = client.get(
        f"{API}/decisions", headers=alice, params={"review_status": ["APPROVED"]}
    ).json()
    assert rv["items"] == []
    bad = client.get(f"{API}/decisions", headers=alice, params={"status": "NOPE"})
    assert bad.status_code == 422
