"""Optional checks against a real PostgreSQL server.

Skipped unless ``JETTAE_TEST_PG_URL`` is set (e.g. ``postgresql+psycopg://...``). The test
drops and recreates the ``public`` schema of that database, so point it at a throw-away DB.
Run locally with pgserver (see docs/runbook.md, "PostgreSQL tests").
"""

from __future__ import annotations

import os
import threading
from datetime import date

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from jt_api_helpers import LEDGER_CSV, FakeClock, FakeIngest, invoice_change, signup, txn_change

from jettae.db import migrate
from jettae.db.config import Settings
from jettae.db.jobs import JobStatus
from jettae.db.runtime import Runtime
from jettae.db.session import make_engine
from jettae.worker import Worker

PG_URL = os.environ.get("JETTAE_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="JETTAE_TEST_PG_URL not set")
API = "/api/v1"


def _reset(url: str) -> None:
    eng = make_engine(url)
    with eng.begin() as c:
        c.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(sa.text("CREATE SCHEMA public"))
    eng.dispose()


@pytest.fixture
def pg_rt(tmp_path):
    assert PG_URL
    _reset(PG_URL)
    migrate.upgrade(PG_URL)
    st = Settings(
        env="test",
        database_url=PG_URL,
        blob_dir=tmp_path / "blobs",
        jwt_secret="pg-test-" + "z" * 40,
        argon2_time_cost=1,
        argon2_memory_kib=8192,
        argon2_parallelism=1,
        job_backoff_base_s=0.0,
    )
    rt = Runtime.build(st, clock=FakeClock(), ingest=FakeIngest())
    yield rt
    rt.close()


def test_pg_migrations_roundtrip():
    assert PG_URL
    _reset(PG_URL)
    migrate.upgrade(PG_URL)
    eng = make_engine(PG_URL)
    try:
        assert migrate.current_revision(eng) == migrate.head_revision()
        with eng.connect() as conn:
            diff = compare_metadata(
                MigrationContext.configure(conn, opts={"compare_type": True}),
                migrate.target_metadata(),
            )
        assert diff == [], diff
        migrate.downgrade(PG_URL, "base")
        assert set(sa.inspect(eng).get_table_names()) - {"alembic_version"} == set()
        migrate.upgrade(PG_URL)
    finally:
        eng.dispose()


def test_pg_api_flow_and_isolation(pg_rt, make_client):
    client = make_client(pg_rt)
    a = signup(client, "a@pg.example", "A")
    b = signup(client, "b@pg.example", "B")
    up = client.post(
        f"{API}/documents",
        headers=a,
        files={"file": ("l.csv", LEDGER_CSV.encode(), "text/csv")},
    )
    assert up.status_code == 201, up.text
    Worker(pg_rt, owner="w").run_once()
    det = client.get(f"{API}/decisions/dec:I1", headers=a).json()
    assert det["status"] == "MATCHED" and det["review_status"] == "VERIFIED"
    assert client.get(f"{API}/decisions/dec:I1", headers=b).status_code == 404
    r = client.post(
        f"{API}/decisions/dec:I1/approvals", headers=a, json={"expected_result_hash": "0" * 64}
    )
    assert r.status_code == 409
    r = client.post(
        f"{API}/decisions/dec:I1/approvals",
        headers=a,
        json={"expected_result_hash": det["result_hash"]},
    )
    assert r.status_code == 201
    assert client.get(f"{API}/ready").status_code == 200


def test_pg_concurrent_claims_and_incremental(pg_rt):
    tenant = "tn_pg"
    with pg_rt.db.write() as s:
        from jettae.db.orm import TenantRow

        s.add(TenantRow(id=tenant, name="pg"))
    jobs = [pg_rt.queue.enqueue(tenant, "run_analysis", {}) for _ in range(20)]
    claimed: list[str] = []
    lock = threading.Lock()

    def grab(owner: str) -> None:
        while True:
            got = pg_rt.queue.claim(owner, limit=3)
            if not got:
                return
            with lock:
                claimed.extend(ls.job_id for ls in got)

    th = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(4)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    assert sorted(claimed) == sorted(j.id for j in jobs)  # each job claimed exactly once
    assert all(pg_rt.queue.get(tenant, j.id).status is JobStatus.RUNNING for j in jobs)

    # incremental recompute through the PostgreSQL ResultStore equals full recompute
    from jettae.db.plain import changes_from_plain

    svc = pg_rt.service
    svc.apply_change(tenant, changes_from_plain([invoice_change(), txn_change()], tenant))
    svc.run_analysis(tenant, as_of=date(2025, 11, 1))
    out = svc.apply_change(
        tenant, changes_from_plain([invoice_change("I2", 5)], tenant), verify_full=True
    )
    assert out.equivalent_to_full is True and out.plan.fallback_full is False


def test_pg_concurrent_refresh_does_not_fork_the_token_family(pg_rt, monkeypatch):
    """Two requests rotating the same refresh token at the same time: at most one gets a
    new token; the other is treated as reuse (row-level atomic consume)."""
    import time

    from jettae.api.auth import AuthService
    from jettae.api.errors import ApiError

    auth = AuthService(pg_rt.db, pg_rt.settings, clock=pg_rt.clock)
    auth.signup("race@pg.example", "correct horse battery", "Race")
    tok = auth.login("race@pg.example", "correct horse battery").refresh_token
    orig = AuthService._issue

    def slow_issue(self, *a, **kw):
        time.sleep(0.5)
        return orig(self, *a, **kw)

    monkeypatch.setattr(AuthService, "_issue", slow_issue)
    out: list[tuple[str, str]] = []
    barrier = threading.Barrier(2)

    def go() -> None:
        barrier.wait()
        try:
            out.append(("ok", auth.refresh(tok).refresh_token))
        except ApiError as e:
            out.append(("err", e.code))

    th = [threading.Thread(target=go) for _ in range(2)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    assert sorted(k for k, _ in out) == ["err", "ok"], out
    assert [c for k, c in out if k == "err"] in (
        ["refresh_token_reused"],
        ["invalid_refresh_token"],
    )


def test_pg_nul_in_uploaded_csv_is_reported_not_crashing(pg_rt, make_client, tmp_path):
    """Same rule on PostgreSQL and SQLite: NUL in a text file -> CORRUPT/FAILED document,
    never a DataError from the database."""
    from jettae.db.ingest_bridge import ModuleIngest

    real = Runtime.build(pg_rt.settings, clock=FakeClock(), ingest=ModuleIngest())
    try:
        client = make_client(real)
        h = signup(client, "nul@pg.example", "N")
        rows = ["거래일자,적요,보낸분/받는분,출금액,입금액,잔액"] + [
            f"2025-09-{(i % 28) + 1:02d},입금,가나유통,0,{1000 + i},{1000 + i}" for i in range(400)
        ]
        text = "\n".join(rows)
        content = (text[:9000] + "\x00" + text[9000:]).encode("utf-8")
        up = client.post(
            f"{API}/documents",
            headers=h,
            files={"file": ("bank.csv", content, "text/csv")},
            data={"kind": "bank"},
        )
        assert up.status_code == 201, up.text
        Worker(real, owner="w").run_once()
        job = client.get(f"{API}/jobs/{up.json()['job_id']}", headers=h).json()
        assert job["status"] == "succeeded", job
        assert job["result"]["document_status"] in ("CORRUPT", "FAILED"), job
    finally:
        real.close()


def test_pg_document_versions_and_evidence_links(pg_rt, make_client):
    """Review fixes on PostgreSQL through the real parser: an old version re-ingested after a
    newer one never replaces the current ledger (head pointer + advisory-locked apply), and a
    user's evidence-link confirmation is stored in the ledger and read back by a full
    recompute."""
    from urllib.parse import quote

    from jettae.db.ingest_bridge import ModuleIngest

    real = Runtime.build(pg_rt.settings, clock=FakeClock(), ingest=ModuleIngest())
    try:
        client = make_client(real)
        h = signup(client, "ver@pg.example", "V")
        tenant = client.get(f"{API}/auth/me", headers=h).json()["tenant_id"]
        header = "거래처,거래형태,발주번호,정산금액,상품수령일\n"

        def upload(text: str, kind: str, name: str, document_id: str | None = None) -> dict:
            data = {"kind": kind}
            if document_id:
                data["document_id"] = document_id
            r = client.post(
                f"{API}/documents",
                headers=h,
                files={"file": (name, text.encode(), "text/csv")},
                data=data,
            )
            assert r.status_code == 201, r.text
            return r.json()

        w = Worker(real, owner="w")
        v1 = upload(header + "가나유통,직매입,PO-1,1000,2025-08-07\n", "settlement", "s.csv")
        w.run_once()
        upload(
            header + "가나유통,직매입,PO-1,2000,2025-08-07\n",
            "settlement",
            "s.csv",
            v1["document"]["document_id"],
        )
        w.run_once()
        retry = client.post(
            f"{API}/jobs",
            headers=h,
            json={"type": "ingest_document", "params": {"doc_version_id": v1["document"]["id"]}},
        )
        assert retry.status_code == 202, retry.text
        w.run_once()
        lines = real.repos.ledger.list(tenant, "settlement_line")
        assert [ln.amount.amount for ln in lines] == [2000]
        head = real.repos.heads.get(tenant, v1["document"]["document_id"])
        assert head is not None and head.version == 2

        upload(
            "작성일자,승인번호,공급받는자상호,합계금액,공급가액,세액\n"
            "2025-08-07,20250807-41000000-12345678,(주)가나유통,2000,1819,181\n",
            "tax_invoice",
            "매출세금계산서.csv",
        )
        w.run_once()
        r = client.post(
            f"{API}/jobs",
            headers=h,
            json={"type": "run_analysis", "params": {"as_of": "2025-11-01"}},
        )
        assert r.status_code == 202
        w.run_once()
        items = client.get(f"{API}/decisions", headers=h).json()["items"]
        (pending,) = [d for d in items if d["counted"] is False]
        out = client.post(
            f"{API}/decisions/{quote(pending['id'], safe='')}/evidence-link",
            headers=h,
            json={
                "expected_result_hash": pending["result_hash"],
                "relation": "same_sale",
                "settlement_line_id": lines[0].id,
            },
        )
        assert out.status_code == 200, out.text
        run = real.service.run_analysis(tenant, as_of=date(2025, 11, 1))
        assert [v.decision.subject_id for v in run.decisions] == [lines[0].id]
    finally:
        real.close()


def test_pg_carry_over_and_duplicate_lines(pg_rt, make_client):
    """Second review on PostgreSQL through the real parser: an unreadable cell in a
    correction keeps the earlier row until the version is acknowledged (removal in the
    acknowledgment transaction), and the same line uploaded in a second document is counted
    once (provenance read back from the database)."""
    from jettae.db.ingest_bridge import ModuleIngest

    real = Runtime.build(pg_rt.settings, clock=FakeClock(), ingest=ModuleIngest())
    try:
        client = make_client(real)
        h = signup(client, "carry@pg.example", "C")
        tenant = client.get(f"{API}/auth/me", headers=h).json()["tenant_id"]
        header = "거래처,거래형태,발주번호,정산금액,상품수령일\n"
        rows = "".join(
            f"가나유통,직매입,PO-{i},{a},2025-08-07\n" for i, a in ((2, 2000), (3, 10), (4, 20))
        )

        def upload(text: str, name: str, document_id: str | None = None) -> dict:
            data = {"kind": "settlement"}
            if document_id:
                data["document_id"] = document_id
            r = client.post(
                f"{API}/documents",
                headers=h,
                files={"file": (name, text.encode(), "text/csv")},
                data=data,
            )
            assert r.status_code == 201, r.text
            return r.json()

        w = Worker(real, owner="w")
        v1 = upload(header + rows, "b.csv")
        w.run_once()
        v2 = upload(
            header + rows.replace("PO-2,2000", "PO-2,금액오류"),
            "b.csv",
            v1["document"]["document_id"],
        )
        w.run_once()
        app = client.get(f"{API}/jobs/{v2['job_id']}", headers=h).json()["result"]["application"]
        assert app["state"] == "applied_needs_ack" and app["records_carried_over"] == 1
        refs = sorted(ln.reference for ln in real.repos.ledger.list(tenant, "settlement_line"))
        assert refs == ["PO-2", "PO-3", "PO-4"]
        ack = client.post(
            f"{API}/document-versions/{v2['document']['id']}/acknowledge",
            headers=h,
            json={"fingerprint": app["fingerprint"]},
        )
        assert ack.status_code == 200 and ack.json()["records_removed"] == 1, ack.text
        refs = sorted(ln.reference for ln in real.repos.ledger.list(tenant, "settlement_line"))
        assert refs == ["PO-3", "PO-4"]

        upload("\ufeff" + header + "가나유통,직매입,PO-3,10,2025-08-07\n", "b_reissued.csv")
        w.run_once()
        run = real.service.run_analysis(tenant, as_of=date(2025, 11, 1))
        pending = [v.decision for v in run.decisions if "duplicate_line" in v.decision.unresolved]
        assert len(pending) == 1 and len(run.decisions) == 3
    finally:
        real.close()
