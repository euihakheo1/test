"""Job lifecycle with the worker: run-once, cancel, retry/backoff, crash resume, limits."""

from __future__ import annotations

import threading
import time

import pytest
from jt_api_helpers import LEDGER_CSV, invoice_change, txn_change

from jettae.db.ingest_bridge import ModuleIngest
from jettae.db.jobs import JobCancelledError, JobStatus, LeaseLostError
from jettae.worker import (
    DEFAULT_HANDLERS,
    PermanentJobError,
    TransientJobError,
    Worker,
    handle_apply_change,
)

API = "/api/v1"


def _tenant(client, headers) -> str:
    return client.get(f"{API}/auth/me", headers=headers).json()["tenant_id"]


def test_lifecycle_via_api(client, alice, worker, clock):
    r = client.post(
        f"{API}/jobs",
        headers=alice,
        json={"type": "apply_change", "params": {"changes": [invoice_change(), txn_change()]}},
    )
    assert r.status_code == 202, r.text
    jid = r.json()["job_id"]
    j = client.get(f"{API}/jobs/{jid}", headers=alice).json()
    assert j["status"] == "queued" and j["attempts"] == 0
    assert worker.run_once() == 1
    j = client.get(f"{API}/jobs/{jid}", headers=alice).json()
    assert j["status"] == "succeeded" and j["attempts"] == 1
    assert j["result"]["changed"] == ["dec:I1"] and j["finished_at"]
    # analysis job summary
    clock.advance(1)
    r = client.post(f"{API}/jobs", headers=alice, json={"type": "run_analysis", "params": {}})
    worker.run_once()
    res = client.get(f"{API}/jobs/{r.json()['job_id']}", headers=alice).json()["result"]
    assert res["decisions"] == 1 and res["by_status"] == {"MATCHED": 1}
    # listing (newest first) with cursor
    page1 = client.get(f"{API}/jobs", headers=alice, params={"limit": 1}).json()
    assert page1["items"][0]["id"] == r.json()["job_id"] and page1["next_cursor"]
    page2 = client.get(
        f"{API}/jobs", headers=alice, params={"limit": 1, "cursor": page1["next_cursor"]}
    ).json()
    assert page2["items"][0]["id"] == jid and page2["next_cursor"] is None
    # nothing left
    assert worker.run_once() == 0


def test_invalid_job_params_are_422(client, alice):
    for body in (
        {"type": "apply_change", "params": {"changes": [{"kind": "add", "entity": "invoice"}]}},
        {"type": "apply_change", "params": {"changes": [{**invoice_change(), "kind": "zap"}]}},
        {"type": "run_analysis", "params": {"as_of": "2025-13-01"}},
        {"type": "run_analysis", "params": {"bogus": 1}},
        {"type": "nope", "params": {}},
    ):
        r = client.post(f"{API}/jobs", headers=alice, json=body)
        assert r.status_code == 422, (body, r.text)
    bad_money = invoice_change()
    bad_money["record"]["amount"] = 10.5
    r = client.post(f"{API}/changes", headers=alice, json={"changes": [bad_money]})
    assert r.status_code == 422 and "float" in r.json()["error"]["message"]


def test_cancel_queued_job(client, alice, worker):
    r = client.post(f"{API}/jobs", headers=alice, json={"type": "run_analysis", "params": {}})
    jid = r.json()["job_id"]
    c = client.post(f"{API}/jobs/{jid}/cancel", headers=alice)
    assert c.status_code == 202 and c.json()["status"] == "cancelled"
    assert worker.run_once() == 0
    assert client.get(f"{API}/jobs/{jid}", headers=alice).json()["status"] == "cancelled"
    # cancelling a finished job leaves it unchanged
    assert client.post(f"{API}/jobs/{jid}/cancel", headers=alice).json()["status"] == "cancelled"


def test_cancel_running_job_rolls_back_its_work(client, alice, rt):
    tenant = _tenant(client, alice)
    job = rt.queue.enqueue(tenant, "apply_change", {"changes": [invoice_change(), txn_change()]})
    started, done = threading.Event(), threading.Event()

    def slow_apply(ctx):
        started.set()
        deadline = time.monotonic() + 10
        while not rt.queue.get(tenant, job.id).cancel_requested:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        return handle_apply_change(ctx)  # completion sees cancel -> transaction rolls back

    w = Worker(rt, owner="w1", handlers={"apply_change": slow_apply}, heartbeat_s=0.05)
    th = threading.Thread(target=lambda: (w.run_once(), done.set()))
    th.start()
    assert started.wait(5)
    c = client.post(f"{API}/jobs/{job.id}/cancel", headers=alice)
    assert c.status_code == 202 and c.json()["cancel_requested"] is True
    th.join(10)
    assert done.is_set()
    final = rt.queue.get(tenant, job.id)
    assert final.status is JobStatus.CANCELLED
    assert rt.repos.ledger.get(tenant, "invoice", "I1") is None  # work rolled back
    assert rt.repos.decisions.current(tenant) == {}


def test_transient_errors_retry_then_succeed(client, alice, rt, clock):
    tenant = _tenant(client, alice)
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientJobError("database is locked (simulated)")
        return {"ok": True}

    job = rt.queue.enqueue(tenant, "run_analysis", {})
    w = Worker(rt, owner="w", handlers={"run_analysis": flaky})
    w.run_once()  # backoff base is 0 in tests -> retried within the same run
    j = rt.queue.get(tenant, job.id)
    assert j.status is JobStatus.SUCCEEDED and j.attempts == 3 and j.result == {"ok": True}
    assert j.error is None


def test_backoff_delays_retry(client, alice, rt, clock):
    tenant = _tenant(client, alice)
    rt.queue.backoff_base_s = 60.0
    job = rt.queue.enqueue(tenant, "run_analysis", {})

    def boom(ctx):
        raise TransientJobError("temporary")

    w = Worker(rt, owner="w", handlers={"run_analysis": boom})
    assert w.run_once() == 1
    j = rt.queue.get(tenant, job.id)
    assert j.status is JobStatus.QUEUED and j.attempts == 1 and j.error["retry"] is True
    delay = (j.run_after - clock()).total_seconds()
    assert 30 <= delay <= 60  # base * 2^0 with jitter in [0.5, 1]
    assert w.run_once() == 0  # not yet due
    clock.advance(61)
    assert w.run_once() == 1
    assert rt.queue.get(tenant, job.id).attempts == 2
    assert 60 <= rt.queue.backoff(2) <= 120 and rt.queue.backoff(30) <= rt.queue.backoff_max_s


def test_attempts_exhausted_and_permanent_errors(client, alice, rt):
    tenant = _tenant(client, alice)

    def always(ctx):
        raise TimeoutError("upstream timeout")

    j1 = rt.queue.enqueue(tenant, "run_analysis", {})
    Worker(rt, owner="w", handlers={"run_analysis": always}).run_once()
    v = rt.queue.get(tenant, j1.id)
    assert v.status is JobStatus.FAILED and v.attempts == rt.settings.job_max_attempts
    assert v.error["code"] == "transient_error"

    def perm(ctx):
        raise PermanentJobError("bad_payload", "nope")

    j2 = rt.queue.enqueue(tenant, "run_analysis", {})
    Worker(rt, owner="w", handlers={"run_analysis": perm}).run_once()
    v2 = rt.queue.get(tenant, j2.id)
    assert v2.status is JobStatus.FAILED and v2.attempts == 1
    assert v2.error == {
        "code": "bad_payload",
        "type": "PermanentJobError",
        "message": "nope",
        "attempt": 1,
    }

    # domain errors are permanent too (e.g. removing a record that does not exist)
    j3 = rt.queue.enqueue(
        tenant, "apply_change", {"changes": [{"kind": "remove", "entity": "invoice", "id": "ZZ"}]}
    )
    Worker(rt, owner="w").run_once()
    v3 = rt.queue.get(tenant, j3.id)
    assert (
        v3.status is JobStatus.FAILED and v3.attempts == 1 and v3.error["type"] == "NotFoundError"
    )


def test_crash_resume_after_lease_expiry(client, alice, rt, clock):
    tenant = _tenant(client, alice)
    job = rt.queue.enqueue(tenant, "apply_change", {"changes": [invoice_change(), txn_change()]})
    # worker "dead" claims the job and crashes (never completes, never heartbeats)
    [lease] = rt.queue.claim("dead-worker")
    assert rt.queue.get(tenant, job.id).status is JobStatus.RUNNING
    w = Worker(rt, owner="alive")
    assert w.run_once() == 0  # lease still valid: nobody else may take it
    clock.advance(rt.settings.job_lease_s + 1)
    assert w.run_once() == 1  # resumed by another worker
    v = rt.queue.get(tenant, job.id)
    assert v.status is JobStatus.SUCCEEDED and v.attempts == 2
    assert rt.repos.decisions.get_current(tenant, "dec:I1") is not None
    # the crashed worker comes back: it cannot overwrite the result
    with pytest.raises(LeaseLostError), rt.db.write(tenant):
        rt.queue.complete(lease, {"stale": True})
    assert rt.queue.heartbeat(lease).alive is False
    assert rt.queue.get(tenant, job.id).result != {"stale": True}


def test_lease_expiry_counts_attempts(client, alice, rt, clock):
    tenant = _tenant(client, alice)
    job = rt.queue.enqueue(tenant, "run_analysis", {}, max_attempts=2)
    for _ in range(2):
        rt.queue.claim("crashy")
        clock.advance(rt.settings.job_lease_s + 1)
    assert rt.queue.claim("next") == []
    v = rt.queue.get(tenant, job.id)
    assert v.status is JobStatus.FAILED and v.error["code"] == "lease_expired"


def test_heartbeat_extends_lease_and_sees_cancel(client, alice, rt, clock):
    tenant = _tenant(client, alice)
    job = rt.queue.enqueue(tenant, "run_analysis", {})
    [lease] = rt.queue.claim("w")
    clock.advance(rt.settings.job_lease_s - 1)
    assert rt.queue.heartbeat(lease).alive
    clock.advance(rt.settings.job_lease_s - 1)
    assert rt.queue.claim("other") == []  # extended
    rt.queue.cancel(tenant, job.id)
    hb = rt.queue.heartbeat(lease)
    assert hb.alive and hb.cancel_requested
    with pytest.raises(JobCancelledError), rt.db.write(tenant):
        rt.queue.complete(lease, {})


def test_bounded_concurrency(client, alice, rt):
    tenant = _tenant(client, alice)
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def slow(ctx):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.15)
        with lock:
            state["now"] -= 1
        return {}

    for _ in range(6):
        rt.queue.enqueue(tenant, "run_analysis", {})
    w = Worker(rt, owner="w", concurrency=2, handlers={"run_analysis": slow})
    assert w.run_once() == 6
    assert state["max"] == 2


def test_ingest_unavailable_fails_with_clear_error(client, alice, rt, settings, clock, make_client):
    from jettae.db.runtime import Runtime

    rt2 = Runtime.build(
        settings, clock=clock, ingest=ModuleIngest("jettae.ingest.no_such_module_xyz")
    )
    up = client.post(
        f"{API}/documents",
        headers=alice,
        files={"file": ("l.csv", LEDGER_CSV.encode(), "text/csv")},
    ).json()
    Worker(rt2, owner="w").run_once()
    j = rt2.queue.get(_tenant(client, alice), up["job_id"])
    assert j.status is JobStatus.FAILED and j.attempts == 1
    assert j.error["code"] == "ingest_unavailable"
    assert "jettae.ingest.no_such_module_xyz" in j.error["message"]
    c2 = make_client(rt2)
    r = c2.get(f"{API}/document-versions/{up['document']['id']}/mapping", headers=alice)
    assert r.status_code == 503 and r.json()["error"]["code"] == "ingest_unavailable"
    rt2.close()


def test_default_handlers_cover_all_job_types():
    from jettae.db.jobs import JOB_TYPES

    # JOB_TYPES lists only the generic POST /jobs types. investigate_decision is internal: it is
    # enqueued together with its agent_investigations row, so it has a handler but no public type.
    assert set(DEFAULT_HANDLERS) == set(JOB_TYPES) | {"investigate_decision"}
