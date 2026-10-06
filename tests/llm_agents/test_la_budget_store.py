"""Shared LLM budget: atomic reservations across objects, threads and OS processes.

Hand-written amounts only (no provider is called). The race tests use a 10 KRW limit and
8 KRW reservations: whatever the interleaving, exactly one reservation may succeed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from la_helpers import gateway

from jettae.llm import (
    Budget,
    BudgetExceeded,
    FakeProvider,
    FileLockBudgetStore,
    LiveCallRefused,
    LLMMessage,
    LLMRequest,
    MemoryBudgetStore,
    SqlBudgetStore,
    budget_from_env,
    strict_object,
)
from jettae.llm.budget import max_cost_krw

PG_URL = os.environ.get("JETTAE_TEST_PG_URL")
SRC = str(Path(__file__).resolve().parents[2] / "src")


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path.as_posix()}/budget.db"


StoreFactory = Callable[[Path], Any]


def _file_store(tmp: Path) -> FileLockBudgetStore:
    return FileLockBudgetStore(tmp / "ledger.jsonl")


def _sql_store(tmp: Path) -> SqlBudgetStore:
    return SqlBudgetStore(_sqlite_url(tmp))


SHARED_STORES = [pytest.param(_file_store, id="filelock"), pytest.param(_sql_store, id="sqlite")]


def _race(budgets: list[Budget], amount: Decimal) -> list[str]:
    """Start every reservation at the same moment; return 'ok' / 'exceeded' per budget."""
    barrier = threading.Barrier(len(budgets))
    out: list[str] = [""] * len(budgets)

    def go(i: int) -> None:
        barrier.wait()
        try:
            budgets[i].reserve(amount, model="m", purpose="race")
            out[i] = "ok"
        except BudgetExceeded:
            out[i] = "exceeded"

    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(budgets))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return out


# ----------------------------------------------------------------------------- objects
@pytest.mark.parametrize("make", SHARED_STORES)
def test_two_live_objects_racing_8_plus_8_on_10(tmp_path: Path, make: StoreFactory) -> None:
    for attempt in range(5):  # several rounds: different interleavings
        d = tmp_path / f"r{attempt}"
        d.mkdir()
        a, b = Budget(Decimal(10), store=make(d)), Budget(Decimal(10), store=make(d))
        assert sorted(_race([a, b], Decimal(8))) == ["exceeded", "ok"]
        # both objects see the other's in-flight reservation
        assert a.reserved_krw == b.reserved_krw == Decimal(8)
        assert a.remaining_krw == b.remaining_krw == Decimal(2)


@pytest.mark.parametrize("make", SHARED_STORES)
def test_many_objects_never_exceed_the_limit(tmp_path: Path, make: StoreFactory) -> None:
    budgets = [Budget(Decimal(10), store=make(tmp_path)) for _ in range(12)]
    res = _race(budgets, Decimal(1)) + _race(budgets, Decimal(1))
    assert res.count("ok") == 10
    assert budgets[0].reserved_krw == Decimal(10) and budgets[5].remaining_krw == 0


def test_memory_store_shared_by_two_objects() -> None:
    store = MemoryBudgetStore()
    a, b = Budget(Decimal(10), store=store), Budget(Decimal(10), store=store)
    assert sorted(_race([a, b], Decimal(8))) == ["exceeded", "ok"]


@pytest.mark.parametrize("make", SHARED_STORES)
def test_settled_spending_of_another_object_is_seen(tmp_path: Path, make: StoreFactory) -> None:
    a, b = Budget(Decimal(10), store=make(tmp_path)), Budget(Decimal(10), store=make(tmp_path))
    r = a.reserve(Decimal(8), model="m", purpose="x")
    a.settle(r, Decimal(8))
    with pytest.raises(BudgetExceeded):
        b.reserve(Decimal(8), model="m", purpose="x")
    assert b.spent_krw == Decimal(8) and b.reserved_krw == 0
    with pytest.raises(RuntimeError):  # settled by a, cannot be settled again through b
        b.settle(r.__class__(**{**r.__dict__, "settled": False}), Decimal(1))


def test_ledger_path_objects_share_one_limit(tmp_path: Path) -> None:
    """The review reproduction: two Budget objects on the same ledger file."""
    path = tmp_path / "ledger.jsonl"
    a, b = Budget(Decimal(10), ledger_path=path), Budget(Decimal(10), ledger_path=path)
    r = a.reserve(Decimal(8), model="m", purpose="x")
    with pytest.raises(BudgetExceeded):  # in flight, not settled yet: still counted
        b.reserve(Decimal(8), model="m", purpose="x")
    a.settle(r, Decimal(3))
    b.reserve(Decimal(7), model="m", purpose="x")
    with pytest.raises(BudgetExceeded):
        a.reserve(Decimal("0.0001"), model="m", purpose="x")


def test_older_ledger_lines_count_as_spent(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({"reservation": "old", "cost_krw": "9.5"}) + "\n", encoding="utf-8")
    b = Budget(Decimal(10), ledger_path=path)
    assert b.spent_krw == Decimal("9.5")
    with pytest.raises(BudgetExceeded):
        b.reserve(Decimal(1), model="m", purpose="x")


# ----------------------------------------------------------------------------- processes
_CHILD = textwrap.dedent(
    """
    import sys, time
    from decimal import Decimal
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from jettae.llm.base import BudgetExceeded
    from jettae.llm.budget import Budget
    from jettae.llm.budget_store import FileLockBudgetStore, SqlBudgetStore
    kind, target, me, bid = sys.argv[2], sys.argv[3], sys.argv[5], sys.argv[6]
    workdir = Path(sys.argv[4])
    if kind == "file":
        store = FileLockBudgetStore(target)
    else:
        store = SqlBudgetStore(target, budget_id=bid)
    budget = Budget(Decimal(10), store=store)
    (workdir / f"ready-{me}").write_text("1")
    while not (workdir / "go").exists():
        time.sleep(0.001)
    try:
        budget.reserve(Decimal(8), model="m", purpose=f"proc-{me}")
        print("ok")
    except BudgetExceeded:
        print("exceeded")
    # exit without settling: the reservation stays in flight (crashed-process case)
    """
)


def _race_processes(
    kind: str, target: str, workdir: Path, n: int = 2, budget_id: str = "default"
) -> list[str]:
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, SRC, kind, target, str(workdir), str(i), budget_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(n)
    ]
    deadline = time.monotonic() + 60
    while not all((workdir / f"ready-{i}").exists() for i in range(n)):
        assert time.monotonic() < deadline, [p.communicate(timeout=5) for p in procs]
        time.sleep(0.01)
    (workdir / "go").write_text("1")
    out = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=60)
        assert p.returncode == 0, stderr
        out.append(stdout.strip())
    return out


@pytest.mark.parametrize("kind", ["file", "sqlite"])
def test_two_os_processes_racing_8_plus_8_on_10(tmp_path: Path, kind: str) -> None:
    if kind == "file":
        target = str(tmp_path / "ledger.jsonl")
        store: Any = FileLockBudgetStore(target)
    else:
        target = _sqlite_url(tmp_path)
        store = SqlBudgetStore(target)  # parent creates the tables first
    for attempt in range(3):
        work = tmp_path / f"w{attempt}"
        work.mkdir()
        res = _race_processes(kind, target, work)
        assert res.count("ok") == (1 if attempt == 0 else 0), res
        assert res.count("exceeded") == (1 if attempt == 0 else 2), res
    # the dead processes' reservation is still counted
    assert Budget(Decimal(10), store=store).reserved_krw == Decimal(8)


@pytest.mark.skipif(not PG_URL, reason="JETTAE_TEST_PG_URL not set")
def test_postgres_objects_and_processes_share_one_limit(tmp_path: Path) -> None:
    assert PG_URL
    bid = f"t{os.getpid()}-{time.time_ns()}"
    a = Budget(Decimal(10), store=SqlBudgetStore(PG_URL, budget_id=bid))
    b = Budget(Decimal(10), store=SqlBudgetStore(PG_URL, budget_id=bid))
    assert sorted(_race([a, b], Decimal(8))) == ["exceeded", "ok"]
    work = tmp_path / "pg"
    work.mkdir()
    # kind "sql" = SqlBudgetStore(url); a fresh budget row raced by two OS processes
    res = _race_processes("sql", PG_URL, work, budget_id=bid + "-p")
    assert sorted(res) == ["exceeded", "ok"]
    assert Budget(Decimal(10), store=SqlBudgetStore(PG_URL, budget_id=bid + "-p")).reserved_krw == 8


# ----------------------------------------------------------------------------- TTL policy
class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 6, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.mark.parametrize("make", SHARED_STORES)
def test_expired_reservation_keeps_counting_until_reconciled(
    tmp_path: Path, make: StoreFactory
) -> None:
    clock = _Clock()
    crashed = Budget(Decimal(10), store=make(tmp_path), reservation_ttl_s=60, clock=clock)
    other = Budget(Decimal(10), store=make(tmp_path), reservation_ttl_s=60, clock=clock)
    r = crashed.reserve(Decimal(8), model="m", purpose="x")
    clock.now += timedelta(seconds=61)
    snap = other.snapshot()
    assert snap["expired_open_krw"] == "8.0000" and snap["remaining_krw"] == "2.0000"
    with pytest.raises(BudgetExceeded):  # expiry alone frees nothing (possibly billed)
        other.reserve(Decimal(8), model="m", purpose="x")
    assert other.reconcile_expired() == [r.id]
    assert other.spent_krw == Decimal(8) and other.reserved_krw == 0
    assert other.snapshot()["possibly_billed_krw"] == "8.0000"
    # the original process was only slow: its late settlement replaces the assumption
    crashed.settle(r, Decimal("1.5"))
    assert other.spent_krw == Decimal("1.5")
    other.reserve(Decimal(8), model="m", purpose="x")
    with pytest.raises(RuntimeError):
        crashed.settle(r, Decimal(1))


def test_zero_limit_refuses_before_reserving(tmp_path: Path) -> None:
    with pytest.raises(LiveCallRefused):
        Budget(Decimal(0), ledger_path=tmp_path / "l.jsonl").reserve(
            Decimal(1), model="m", purpose="x"
        )
    assert not (tmp_path / "l.jsonl").exists()


# ----------------------------------------------------------------------------- env / gateway
def test_budget_from_env_selects_a_shared_store(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    state = tmp_path / "state"
    live = budget_from_env(
        {"JETTAE_LLM_BUDGET_KRW": "10", "JETTAE_STATE_DIR": str(state)}, live=True
    )
    assert isinstance(live.store, FileLockBudgetStore)
    # absolute and independent of the working directory (second review, finding 7)
    assert live.store.path == state / "llm_budget.jsonl" and live.store.path.is_absolute()
    assert isinstance(budget_from_env({}, live=False).store, MemoryBudgetStore)
    db = budget_from_env(
        {"JETTAE_LLM_BUDGET_KRW": "10", "JETTAE_LLM_BUDGET_DB": _sqlite_url(tmp_path)}, live=True
    )
    assert isinstance(db.store, SqlBudgetStore) and db.limit_krw == 10


def _req() -> LLMRequest:
    return LLMRequest(
        purpose="test",
        system="s",
        messages=(LLMMessage.user("hello"),),
        schema=strict_object({"answer": {"type": "string"}}),
        tenant_id="t1",
        contains_tenant_data=False,
        prompt_id="p",
        max_output_tokens=50,
    )


def test_unexpected_provider_failure_is_settled_as_possibly_billed() -> None:
    class Boom(FakeProvider):
        def complete(self, request: LLMRequest, *, timeout_s: float) -> Any:
            raise KeyError("sdk bug after the request was sent")

    fake = Boom([])
    gw = gateway(fake, budget_krw="100")
    with pytest.raises(KeyError):
        gw.complete(_req())
    reserved = max_cost_krw(_req(), gw.prices.get(fake.model))  # type: ignore[arg-type]
    assert gw.budget is not None
    assert gw.budget.reserved_krw == 0
    assert gw.budget.spent_krw == reserved.quantize(Decimal("0.0001"), rounding="ROUND_CEILING")
