"""SQL repositories behave like the in-memory adapters (same service results, hashes,
history, approval semantics); codec round-trips preserve content hashes."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime

import pytest

from jettae.app import JettaeService, in_memory_repositories
from jettae.db import migrate
from jettae.db.codec import CodecError, decode, decode_as, encode
from jettae.db.orm import TenantRow
from jettae.db.repos import sql_repositories
from jettae.db.session import Database
from jettae.domain import (
    Agreement,
    BankTxn,
    Change,
    ChangeKind,
    DocKind,
    Fact,
    Invoice,
    Money,
    NotFoundError,
    ReviewStatus,
    SourceSpan,
    StaleResultError,
    TenantMismatchError,
    TradeType,
)
from jettae.domain.hashing import content_hash
from jettae.store.files import FileBlobStore

T = "t1"
NOW = datetime(2025, 11, 1, tzinfo=UTC)


def _inv(iid: str, amount: int, tenant: str = T, received: date | None = date(2025, 8, 7)):
    return Invoice(
        id=iid,
        tenant_id=tenant,
        counterparty="가나유통",
        amount=Money(amount),
        trade_type=TradeType.DIRECT,
        goods_received_date=received,
        facts=(f"f-{iid}",),
    )


def _txn(tid: str, amount: int, d: date, tenant: str = T):
    return BankTxn(
        id=tid,
        tenant_id=tenant,
        booked_date=d,
        amount=Money(amount),
        counterparty="(주)가나유통",
        facts=(f"f-{tid}",),
    )


def _fact(fid: str, subject: str, value: int, doc: str) -> Fact:
    return Fact(
        id=fid,
        tenant_id=T,
        kind="amount",
        value=value,
        span=SourceSpan(doc, {"sheet": "S", "row": 2, "col": "D"}, str(value)),
        extractor="test",
        observed_at=NOW,
        subject_id=subject,
    )


@pytest.fixture
def sql_repos(tmp_path):
    url = f"sqlite:///{tmp_path.as_posix()}/r.db"
    migrate.upgrade(url)
    db = Database(url)
    with db.write() as s:
        s.add(TenantRow(id=T, name="t1"))
        s.add(TenantRow(id="t2", name="t2"))
    yield sql_repositories(db, FileBlobStore(tmp_path / "blobs"))
    db.dispose()


def _scenario(repos):
    counter = iter(range(10_000))
    s = JettaeService(repos, clock=lambda: NOW, id_factory=lambda p: f"{p}_{next(counter)}")
    doc = s.register_document(
        T,
        filename="bank.csv",
        content=b"date,amount\n2025-10-20,10000000\n",
        media_type="text/csv",
        kind=DocKind.BANK,
        text="2025-10-20,10000000 300000",
    )
    s.record_facts(
        T,
        facts=[
            _fact("f-I1", "I1", 10_000_000, doc.id),
            _fact("f-T1", "T1", 10_000_000, doc.id),
        ],
        records=[_inv("I1", 10_000_000), _txn("T1", 10_000_000, date(2025, 10, 20))],
    )
    s.run_analysis(T, as_of=date(2025, 11, 1))
    return s


def test_sql_matches_memory_including_incremental(sql_repos):
    mem, sql = _scenario(in_memory_repositories()), _scenario(sql_repos)
    for svc in (mem, sql):
        d = svc.repos.decisions.get_current(T, "dec:I1")
        svc.approve(T, "dec:I1", d.result_hash, "kim")
    changes = [
        Change(ChangeKind.ADD, "agreement", "A1", Agreement("A1", T, "가나유통", rollover=True)),
        Change(ChangeKind.ADD, "invoice", "I2", _inv("I2", 300_000, received=None)),
    ]
    outs = [svc.apply_change(T, changes, verify_full=True) for svc in (mem, sql)]
    assert outs[0].equivalent_to_full and outs[1].equivalent_to_full
    assert not outs[1].plan.fallback_full  # the SQL ResultStore round-trip kept the graph
    assert outs[0].changed == outs[1].changed and outs[0].review_required == ("dec:I1",)
    assert outs[1].review_required == ("dec:I1",)
    assert outs[0].snapshot_hash == outs[1].snapshot_hash
    for did in ("dec:I1", "dec:I2"):
        vm, vs = mem.decision_view(T, did), sql.decision_view(T, did)
        assert content_hash(vm.decision) == content_hash(vs.decision)
        assert vm.explanation == vs.explanation
        assert vm.review_status == vs.review_status
        assert [c.passed for c in vm.checks] == [c.passed for c in vs.checks]
        assert len(mem.repos.decisions.history(T, did)) == len(sql.repos.decisions.history(T, did))
    assert sql.decision_view(T, "dec:I1").review_status is ReviewStatus.REVIEW_REQUIRED
    loaded = sql.repos.results.load(T)
    assert loaded is not None
    res, _cfg = loaded
    snap = sql.snapshot(T, _cfg)
    from jettae.evidence.engine import full_recompute

    assert full_recompute(snap).comparable() == res.comparable()
    assert full_recompute(snap).graph == res.graph

    # removal -> superseded, history kept
    for svc in (mem, sql):
        svc.apply_change(T, [Change(ChangeKind.REMOVE, "invoice", "I2")])
        assert svc.decision_view(T, "dec:I2").review_status is ReviewStatus.SUPERSEDED
        with pytest.raises(NotFoundError):
            svc.approve(T, "dec:I2", "x", "kim")
    assert sql.repos.decisions.is_superseded(T, "dec:I2")
    assert [r.superseded for r in sql.repos.decisions.history(T, "dec:I2")] == [False, True]


def test_sql_tenant_scoping_and_document_versions(sql_repos):
    s = JettaeService(sql_repos, clock=lambda: NOW)
    a = s.register_document(
        T, filename="x.csv", content=b"1", media_type="text/csv", document_id="D"
    )
    same = s.register_document(
        T, filename="x.csv", content=b"1", media_type="text/csv", document_id="D"
    )
    b = s.register_document(
        T, filename="x.csv", content=b"2", media_type="text/csv", document_id="D"
    )
    assert same.id == a.id and b.version == 2 and b.supersedes == a.id
    assert sql_repos.blobs.get(T, b.storage_key) == b"2"
    assert sql_repos.blobs.get("t2", b.storage_key) is None
    assert sql_repos.documents.get("t2", a.id) is None
    with pytest.raises(TenantMismatchError):
        s.record_facts(T, records=[_inv("Z", 1, tenant="t2")])
    with pytest.raises(TenantMismatchError):
        sql_repos.decisions.save_current("t2", {"x": _scenario_decision()}, NOW)


def _scenario_decision():
    mem = _scenario(in_memory_repositories())
    return mem.repos.decisions.get_current(T, "dec:I1")


def test_sql_approval_race_with_change(sql_repos):
    svc = _scenario(sql_repos)
    for k in range(5):
        cur = svc.repos.decisions.get_current(T, "dec:I1")
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def approver(h=cur.result_hash, barrier=barrier, errors=errors):
            barrier.wait()
            try:
                svc.approve(T, "dec:I1", h, "kim")
            except StaleResultError as e:
                errors.append(e)

        def changer(k=k, barrier=barrier):
            barrier.wait()
            t = _txn(f"TX{k}", 1, date(2025, 10, 25))
            svc.apply_change(T, [Change(ChangeKind.ADD, "bank_txn", t.id, t)])

        th = [threading.Thread(target=approver), threading.Thread(target=changer)]
        for x in th:
            x.start()
        for x in th:
            x.join()
        now = svc.repos.decisions.get_current(T, "dec:I1")
        assert now.result_hash != cur.result_hash
        approvals = svc.repos.approvals.list_for(T, "dec:I1")
        status = svc.decision_view(T, "dec:I1").review_status
        assert status is ReviewStatus.REVIEW_REQUIRED or not approvals
        assert all(a.result_hash != now.result_hash for a in approvals)
        assert len(errors) in (0, 1)


def test_codec_roundtrip_and_rejections():
    inv = _inv("I1", 5)
    data = encode(inv)
    back = decode_as(Invoice, data)
    assert back == inv and content_hash(back) == content_hash(inv)
    assert decode(date | None, None) is None
    with pytest.raises(CodecError):
        decode(Invoice, {**data, "$type": "BankTxn"})
    with pytest.raises(CodecError):
        decode(int, 1.5)
    with pytest.raises(CodecError):
        decode(object, {"$type": "os.system"})
    with pytest.raises(TypeError):
        encode({"x": 1.0})
