"""F1: an older document version never replaces the current ledger or approval state.

upload -> real parser -> worker -> ledger, for: v2 applied then v1 retried; v2 finishing
before v1 (real concurrent worker threads); the same job re-run."""

from __future__ import annotations

import threading
import time
from typing import Any

from jettae.app.contracts import MappingRequest, ParseResult
from jettae.db.ingest_bridge import ModuleIngest
from jettae.domain.models import DocumentVersion
from jettae.worker import Worker

HEADER = "거래처,거래형태,발주번호,정산금액,상품수령일\n"
BANK = (
    "거래일자,적요,보낸분/받는분,출금액,입금액,잔액\n"
    '2025-10-20,타행입금,가나유통,0,"2,000","2,000"\n'
)


def v(amount: int, ref: str = "PO-1") -> str:
    return HEADER + f"가나유통,직매입,{ref},{amount},2025-08-07\n"


def amounts(rt: Any, tenant: str) -> list[int]:
    return sorted(r.amount.amount for r in rt.repos.ledger.list(tenant, "settlement_line"))


def test_v1_retry_after_v2_keeps_ledger_and_approval(api, rt, tenant, work):
    v1 = api.upload(v(1000))
    work()
    doc_id = v1["document"]["document_id"]
    v2 = api.upload(v(2000, "PO-2"), document_id=doc_id)
    api.upload(BANK, kind="bank", name="bank.csv")
    work()
    assert amounts(rt, tenant) == [2000]
    api.analyze()
    work()
    (line,) = rt.repos.ledger.list(tenant, "settlement_line")
    d = api.decision(line.id)
    assert api.approve(line.id, d["result_hash"]).status_code == 201

    job = api.ingest(v1["document"]["id"])  # user / operator re-runs the old version
    work()
    res = api.job(job)
    assert res["status"] == "succeeded", res
    app = res["result"]["application"]
    assert app["state"] == "not_promoted_older"
    assert app["current_version"] == 2 and app["records_removed"] == 0
    assert "오래된 버전" in app["message"]
    assert amounts(rt, tenant) == [2000]  # PO-1 (v1 only) was not restored
    after = api.decision(line.id)
    assert after["result_hash"] == d["result_hash"]
    assert after["review_status"] == "APPROVED"
    # the old version keeps its own parse summary for inspection
    old = api.version(v1["document"]["id"])
    assert old["apply_state"] == "not_promoted_older" and old["is_current"] is False
    assert old["status_detail"]["counts"]["applied_rows"] == 1
    assert old["current_version"] == 2
    listing = api.versions(doc_id)
    assert listing["current"]["doc_version_id"] == v2["document"]["id"]
    assert [x["is_current"] for x in listing["items"]] == [False, True]


class _GatedIngest:
    """Real parser; parsing v1 waits until the v2 job has committed (v1 finishes late)."""

    def __init__(self, rt: Any, tenant: str) -> None:
        self.inner = ModuleIngest()
        self.rt, self.tenant = rt, tenant
        self.v2_job: str | None = None
        self.v1_id: str | None = None
        self.waited = threading.Event()

    def parse(
        self, doc: DocumentVersion, content: bytes, mapping: MappingRequest | None
    ) -> ParseResult:
        out = self.inner.parse(doc, content, mapping)
        if doc.id == self.v1_id:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                j = self.rt.queue.get(self.tenant, self.v2_job)
                if j is not None and j.status.value == "succeeded":
                    self.waited.set()
                    break
                time.sleep(0.02)
        return out

    def suggest_mapping(self, doc: DocumentVersion, content: bytes) -> Any:
        return self.inner.suggest_mapping(doc, content)


def test_v2_finishing_before_v1_wins(api, rt, tenant):
    v1 = api.upload(v(1000))
    v2 = api.upload(v(2000, "PO-2"), document_id=v1["document"]["document_id"])
    gate = _GatedIngest(rt, tenant)
    gate.v1_id, gate.v2_job = v1["document"]["id"], v2["job_id"]
    rt.ingest = gate
    Worker(rt, owner="w-late", concurrency=2, heartbeat_s=0.05).run_once()
    assert gate.waited.is_set(), "v1's parse did not overlap v2's commit"
    r1, r2 = api.job(v1["job_id"]), api.job(v2["job_id"])
    assert r2["result"]["application"]["state"] == "applied"
    assert r1["result"]["application"]["state"] == "not_promoted_older"
    assert amounts(rt, tenant) == [2000]
    head = rt.heads.get(tenant, v1["document"]["document_id"])
    assert head is not None and head.version == 2


def test_same_version_rerun_is_unchanged(api, rt, tenant, work):
    v1 = api.upload(v(1000))
    api.upload(BANK.replace("2,000", "1,000"), kind="bank", name="bank.csv")
    work()
    api.analyze()
    work()
    (line,) = rt.repos.ledger.list(tenant, "settlement_line")
    d = api.decision(line.id)
    assert api.approve(line.id, d["result_hash"]).status_code == 201
    for _ in range(2):
        job = api.ingest(v1["document"]["id"])
        work()
        app = api.job(job)["result"]["application"]
        assert app["state"] == "unchanged", app
        assert app["records_added"] == app["records_updated"] == app["records_removed"] == 0
    assert amounts(rt, tenant) == [1000]
    after = api.decision(line.id)
    assert after["result_hash"] == d["result_hash"] and after["review_status"] == "APPROVED"


def test_crash_after_apply_rolls_back_and_retry_applies_once(api, rt, tenant, work):
    """The ledger change and the job's 'succeeded' mark commit together: a failure at the
    end of the transaction leaves no trace, the retry applies the version once."""
    v1 = api.upload(v(1000))
    real_complete = rt.queue.complete
    calls = {"n": 0}

    def flaky(lease: Any, result: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("simulated crash before commit")
        return real_complete(lease, result)

    rt.queue.complete = flaky  # type: ignore[method-assign]
    try:
        work()
    finally:
        rt.queue.complete = real_complete  # type: ignore[method-assign]
    res = api.job(v1["job_id"])
    assert res["status"] == "succeeded" and res["attempts"] == 2, res
    assert res["result"]["application"]["state"] == "applied"
    assert amounts(rt, tenant) == [1000]
