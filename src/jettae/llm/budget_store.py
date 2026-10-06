"""Shared, atomic storage for the LLM budget (reservations + settlements).

Why a store: several ``Budget`` objects (threads, worker processes, CLI runs) may spend the
same KRW limit. The check "does this reservation fit?" and the write "record the reservation"
must therefore happen in ONE critical section that every spender shares; a per-object
counter (the earlier design) lets two objects each see the full limit.

What counts against the limit (``committed``)::

    committed = sum(cost of settled entries)
              + sum(reserved amount of open entries)      # in flight, live or expired
              + sum(reserved amount of reconciled entries) # expired, assumed billed

Policy for crashed processes and possibly-billed failures (conservative):

- every reservation carries ``expires_at`` (created + TTL). A reservation that passes its TTL
  without a settlement belongs to a process that crashed or hung; the provider may still
  have billed the call, so it **keeps counting at its full reserved amount**. Expiry never
  frees budget by itself.
- :meth:`BudgetStore.reconcile_expired` turns expired open entries into ``reconciled``
  entries whose cost is the reserved amount ("possibly billed"). They keep counting.
- a late :meth:`BudgetStore.settle` from the original process (it was slow, not dead) still
  wins: a ``reconciled`` entry is replaced by the actual cost. A ``settled`` entry can never
  be settled twice (``RuntimeError``).
- the gateway settles a failure that may have reached the model (timeout, 5xx, unexpected
  exception) at the full reserved amount, and only a failure known not to be billed
  (rate limit before acceptance, 4xx) at 0.

Backends:

- :class:`MemoryBudgetStore` -- one process (threads share it). Tests and offline use.
- :class:`FileLockBudgetStore` -- a JSON-lines ledger guarded by an OS file lock
  (``msvcrt`` on Windows, ``fcntl.flock`` on POSIX) around read-check-append. Safe across
  objects and processes on one machine; intended for CLI runs.
- :class:`SqlBudgetStore` -- tables in the service database. The check-and-insert runs in
  one transaction that first locks the budget row: SQLite ``BEGIN IMMEDIATE`` (database
  write lock), PostgreSQL ``SELECT ... FOR UPDATE`` on ``llm_budget``. Safe across processes
  and hosts sharing the database.

Amounts are stored rounded **up** to 0.0001 KRW (the SQL store as integer units of
0.0001 KRW so that sums are exact on every database).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from jettae.llm.base import LLMError

STEP = Decimal("0.0001")


class BudgetStoreCorrupt(LLMError):
    """The budget ledger cannot be read (a damaged line that is not the unfinished last
    write). Spending is refused until the ledger is repaired: guessing the total could let
    paid calls exceed the limit."""


_E4 = Decimal(10_000)

EntryStatus = Literal["open", "settled", "reconciled"]


def q4(amount: Decimal) -> Decimal:
    """Round up to 0.0001 KRW (never under-count spending)."""
    return amount.quantize(STEP, rounding=ROUND_CEILING)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class Reservation:
    """A reserved upper bound for one provider attempt. ``settled`` is local bookkeeping
    of the object that made it; the store is the source of truth."""

    id: str
    amount_krw: Decimal
    model: str
    purpose: str
    created_at: datetime
    expires_at: datetime
    settled: bool = False


@dataclass(frozen=True)
class BudgetTotals:
    """``spent_krw``: settled costs + reconciled (possibly billed) amounts.
    ``reserved_krw``: open reservations, live or expired (expired ones are a subset, shown
    separately in ``expired_open_krw``)."""

    spent_krw: Decimal
    reserved_krw: Decimal
    expired_open_krw: Decimal
    possibly_billed_krw: Decimal
    open_count: int
    expired_open_count: int

    @property
    def committed_krw(self) -> Decimal:
        return self.spent_krw + self.reserved_krw


class BudgetStore(Protocol):
    """Atomic budget ledger shared by every ``Budget`` that points at it."""

    def try_reserve(self, r: Reservation, *, limit_krw: Decimal) -> Decimal | None:
        """Atomically record ``r`` if ``committed + r.amount_krw <= limit_krw``.

        Returns ``None`` on success, otherwise the remaining amount (nothing is written)."""
        ...

    def settle(self, reservation_id: str, cost_krw: Decimal, *, note: str, now: datetime) -> None:
        """Record the actual cost. ``RuntimeError`` if unknown or already settled."""
        ...

    def totals(self, *, now: datetime) -> BudgetTotals: ...

    def reconcile_expired(self, *, now: datetime, note: str) -> list[str]:
        """Mark open reservations past ``expires_at`` as possibly billed (cost = reserved)."""
        ...


# ----------------------------------------------------------------------------- shared fold
@dataclass
class _Entry:
    id: str
    reserved: Decimal
    cost: Decimal | None
    status: EntryStatus
    expires_at: datetime | None


def _totals(entries: Mapping[str, _Entry], extra_spent: Decimal, now: datetime) -> BudgetTotals:
    spent = extra_spent
    reserved = expired = billed = Decimal(0)
    n_open = n_expired = 0
    for e in entries.values():
        if e.status == "open":
            reserved += e.reserved
            n_open += 1
            if e.expires_at is not None and e.expires_at <= now:
                expired += e.reserved
                n_expired += 1
        else:
            spent += e.cost if e.cost is not None else e.reserved
            if e.status == "reconciled":
                billed += e.reserved
    return BudgetTotals(spent, reserved, expired, billed, n_open, n_expired)


# ----------------------------------------------------------------------------- memory
class MemoryBudgetStore:
    """Process-local store (a lock + a dict). Not shared across processes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def try_reserve(self, r: Reservation, *, limit_krw: Decimal) -> Decimal | None:
        with self._lock:
            t = _totals(self._entries, Decimal(0), r.created_at)
            remaining = limit_krw - t.committed_krw
            if q4(r.amount_krw) > remaining:
                return remaining
            self._entries[r.id] = _Entry(r.id, q4(r.amount_krw), None, "open", r.expires_at)
            return None

    def settle(self, reservation_id: str, cost_krw: Decimal, *, note: str, now: datetime) -> None:
        with self._lock:
            e = self._entries.get(reservation_id)
            if e is None or e.status == "settled":
                raise RuntimeError(f"reservation {reservation_id} unknown or already settled")
            e.status, e.cost = "settled", q4(cost_krw)

    def totals(self, *, now: datetime) -> BudgetTotals:
        with self._lock:
            return _totals(self._entries, Decimal(0), now)

    def reconcile_expired(self, *, now: datetime, note: str) -> list[str]:
        with self._lock:
            out = []
            for e in self._entries.values():
                if e.status == "open" and e.expires_at is not None and e.expires_at <= now:
                    e.status, e.cost = "reconciled", e.reserved
                    out.append(e.id)
            return out


# ----------------------------------------------------------------------------- file lock
@contextmanager
def file_lock(path: Path, *, timeout_s: float = 60.0) -> Iterator[None]:
    """Exclusive OS lock on ``path`` (created if missing), shared by every open handle --
    other threads, other ``Budget`` objects and other processes on this machine."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    with path.open("a+b") as fh:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"budget ledger lock {path} not acquired") from None
                    time.sleep(0.005)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"budget ledger lock {path} not acquired") from None
                    time.sleep(0.005)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class FileLockBudgetStore:
    """JSON-lines ledger + OS file lock (``<ledger>.lock``).

    Events: ``{"event": "reserve", ...}``, ``{"event": "settle", "cost_krw": ...}``,
    ``{"event": "reconcile", ...}``. Lines without ``event`` (the older ledger format, one
    line per settled call with ``cost_krw``) count as settled spending. Every operation
    reads the whole ledger under the lock, so a new ``Budget`` object never works from a
    stale total."""

    def __init__(self, path: Path | str, *, lock_timeout_s: float = 60.0) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.lock_timeout_s = lock_timeout_s

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with file_lock(self.lock_path, timeout_s=self.lock_timeout_s):
            yield

    def _events(self) -> list[dict[str, Any]]:
        """Ledger events; call with the lock held.

        A process killed during :meth:`_append` (power loss, kill before the line was
        complete or fsynced) leaves an *unfinished last line*: no trailing newline, or a
        last line that does not parse. It is truncated here, before anything else is read
        or appended. That is safe: an unfinished ``reserve`` line means ``try_reserve`` never
        returned, so no provider call was made; an unfinished ``settle`` line leaves the
        reservation open, which keeps counting at its full reserved amount; an unfinished
        ``reconcile`` changes nothing that counts. A damaged line *before* the last one is
        not an unfinished write: :class:`BudgetStoreCorrupt` (an ``LLMError``) is raised."""
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        keep = len(data)
        if data and not data.endswith(b"\n"):
            keep = data.rfind(b"\n") + 1  # drop the unterminated tail
        # (start offset, raw line) of every newline-terminated line
        lines: list[tuple[int, bytes]] = []
        pos = 0
        while pos < keep:
            end = data.index(b"\n", pos)
            lines.append((pos, data[pos:end]))
            pos = end + 1
        last = max((i for i, (_, raw) in enumerate(lines) if raw.strip()), default=-1)
        events: list[dict[str, Any]] = []
        for i, (start, raw) in enumerate(lines):
            if not raw.strip():
                continue
            try:
                ev = json.loads(raw.decode("utf-8"))
                if not isinstance(ev, dict):
                    raise ValueError("not an object")
            except ValueError as e:  # JSONDecodeError and UnicodeDecodeError included
                if i == last:
                    keep = start  # complete-looking but unparseable last line: unfinished
                    break
                raise BudgetStoreCorrupt(
                    f"budget ledger {self.path} line {i + 1} is damaged ({e}); repair or move "
                    "the file before spending again"
                ) from None
            events.append(ev)
        if keep < len(data):
            with self.path.open("r+b") as fh:
                fh.truncate(keep)
                fh.flush()
                os.fsync(fh.fileno())
        return events

    def _fold(self) -> tuple[dict[str, _Entry], Decimal]:
        entries: dict[str, _Entry] = {}
        legacy = Decimal(0)
        for ev in self._events():
            kind = ev.get("event")
            rid = str(ev.get("reservation", ""))
            if kind is None:  # older format: settled call
                legacy += Decimal(str(ev["cost_krw"]))
            elif kind == "reserve":
                entries[rid] = _Entry(
                    rid,
                    Decimal(str(ev["reserved_krw"])),
                    None,
                    "open",
                    _parse_dt(ev.get("expires_at")),
                )
            elif kind == "settle" and rid in entries:
                entries[rid].status = "settled"
                entries[rid].cost = Decimal(str(ev["cost_krw"]))
            elif kind == "reconcile" and rid in entries:
                entries[rid].status = "reconciled"
                entries[rid].cost = entries[rid].reserved
        return entries, legacy

    def _append(self, events: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def try_reserve(self, r: Reservation, *, limit_krw: Decimal) -> Decimal | None:
        amount = q4(r.amount_krw)
        with self._locked():
            entries, legacy = self._fold()
            remaining = limit_krw - _totals(entries, legacy, r.created_at).committed_krw
            if amount > remaining:
                return remaining
            self._append(
                [
                    {
                        "event": "reserve",
                        "at": _iso(r.created_at),
                        "reservation": r.id,
                        "model": r.model,
                        "purpose": r.purpose,
                        "reserved_krw": str(amount),
                        "expires_at": _iso(r.expires_at),
                        "pid": os.getpid(),
                    }
                ]
            )
            return None

    def settle(self, reservation_id: str, cost_krw: Decimal, *, note: str, now: datetime) -> None:
        with self._locked():
            entries, _ = self._fold()
            e = entries.get(reservation_id)
            if e is None or e.status == "settled":
                raise RuntimeError(f"reservation {reservation_id} unknown or already settled")
            self._append(
                [
                    {
                        "event": "settle",
                        "at": _iso(now),
                        "reservation": reservation_id,
                        "reserved_krw": str(e.reserved),
                        "cost_krw": str(q4(cost_krw)),
                        "note": note,
                        "after_reconcile": e.status == "reconciled",
                    }
                ]
            )

    def totals(self, *, now: datetime) -> BudgetTotals:
        with self._locked():
            entries, legacy = self._fold()
        return _totals(entries, legacy, now)

    def reconcile_expired(self, *, now: datetime, note: str) -> list[str]:
        with self._locked():
            entries, _ = self._fold()
            ids = [
                e.id
                for e in entries.values()
                if e.status == "open" and e.expires_at is not None and e.expires_at <= now
            ]
            self._append(
                [
                    {"event": "reconcile", "at": _iso(now), "reservation": i, "note": note}
                    for i in ids
                ]
            )
            return ids


# ----------------------------------------------------------------------------- SQL
def _sql() -> Any:
    import sqlalchemy as sa

    return sa


_METADATA: Any = None


def budget_tables() -> tuple[Any, Any, Any]:
    """``(metadata, llm_budget, llm_budget_entry)`` -- a separate ``MetaData`` so the
    service's migration metadata is unaffected until a migration adopts these tables."""
    global _METADATA
    sa = _sql()
    if _METADATA is None:
        md = sa.MetaData()
        sa.Table(
            "llm_budget",
            md,
            sa.Column("budget_id", sa.String(64), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        sa.Table(
            "llm_budget_entry",
            md,
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("budget_id", sa.String(64), nullable=False, index=True),
            sa.Column("model", sa.String(200), nullable=False),
            sa.Column("purpose", sa.String(200), nullable=False),
            sa.Column("reserved_e4", sa.BigInteger, nullable=False),  # 0.0001 KRW units
            sa.Column("cost_e4", sa.BigInteger, nullable=True),
            sa.Column("status", sa.String(16), nullable=False),  # open|settled|reconciled
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("owner", sa.String(200), nullable=False, server_default=""),
            sa.Column("note", sa.Text, nullable=False, server_default=""),
        )
        _METADATA = md
    return _METADATA, _METADATA.tables["llm_budget"], _METADATA.tables["llm_budget_entry"]


def _e4(amount: Decimal) -> int:
    return int(q4(amount) * _E4)


def _from_e4(units: int | None) -> Decimal:
    return (Decimal(int(units or 0)) / _E4).quantize(STEP)


def _naive_utc(dt: datetime) -> datetime:
    """SQLite keeps no offset; store every timestamp as UTC so comparisons stay valid."""
    return dt.astimezone(UTC).replace(tzinfo=None)


class SqlBudgetStore:
    """Budget tables in the service database (SQLite or PostgreSQL).

    ``url_or_engine``: a SQLAlchemy URL (an engine is created with
    :func:`jettae.db.session.make_engine`) or an existing engine. Tables are created lazily
    with ``create_all`` (``checkfirst``) unless ``create_tables=False`` (when a migration
    owns them)."""

    def __init__(
        self,
        url_or_engine: Any,
        *,
        budget_id: str = "default",
        create_tables: bool = True,
        owner: str = "",
    ) -> None:
        sa = _sql()
        if isinstance(url_or_engine, str):
            from jettae.db.session import make_engine

            self.engine = make_engine(url_or_engine)
            self._own_engine = True
        else:
            self.engine = url_or_engine
            self._own_engine = False
        self.budget_id = budget_id
        self.owner = owner or f"pid:{os.getpid()}"
        self._md, self._budget, self._entry = budget_tables()
        self._sqlite = self.engine.dialect.name == "sqlite"
        # jettae.db.session engines emit "BEGIN <mode>" from this execution option
        self._write = self.engine.execution_options(jettae_sqlite_begin="IMMEDIATE")
        self._sa = sa
        if create_tables:
            self._create_tables()

    def _create_tables(self) -> None:
        """``create_all`` that tolerates another process creating the tables at the same
        moment (SQLite: serialised by ``BEGIN IMMEDIATE``; PostgreSQL: re-check on error)."""
        sa = self._sa
        try:
            with self._write.begin() as conn:
                self._md.create_all(conn, checkfirst=True)
        except sa.exc.DBAPIError:
            names = set(sa.inspect(self.engine).get_table_names())
            if not {"llm_budget", "llm_budget_entry"} <= names:
                raise

    def close(self) -> None:
        if self._own_engine:
            self.engine.dispose()

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        """One write transaction holding the budget row lock."""
        sa = self._sa
        with self._write.connect() as conn, conn.begin():
            if self._sqlite:
                raw = conn.connection.dbapi_connection
                if not getattr(raw, "in_transaction", True):
                    # engine without the jettae BEGIN hook: take the write lock ourselves
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
            b = self._budget
            row = conn.execute(
                sa.select(b.c.budget_id).where(b.c.budget_id == self.budget_id).with_for_update()
            ).first()
            if row is None:
                if self.engine.dialect.name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    conn.execute(
                        pg_insert(b)
                        .values(budget_id=self.budget_id, created_at=datetime.now(UTC))
                        .on_conflict_do_nothing()
                    )
                else:
                    conn.execute(
                        b.insert().values(
                            budget_id=self.budget_id, created_at=_naive_utc(datetime.now(UTC))
                        )
                    )
                conn.execute(
                    sa.select(b.c.budget_id)
                    .where(b.c.budget_id == self.budget_id)
                    .with_for_update()
                ).first()
            yield conn

    def _ts(self, dt: datetime) -> datetime:
        return _naive_utc(dt) if self._sqlite else dt.astimezone(UTC)

    def _committed_e4(self, conn: Any) -> int:
        sa, e = self._sa, self._entry
        expr = sa.case((e.c.status == "open", e.c.reserved_e4), else_=e.c.cost_e4)
        return int(
            conn.execute(
                sa.select(sa.func.coalesce(sa.func.sum(expr), 0)).where(
                    e.c.budget_id == self.budget_id
                )
            ).scalar_one()
        )

    def try_reserve(self, r: Reservation, *, limit_krw: Decimal) -> Decimal | None:
        amount = _e4(r.amount_krw)
        with self._tx() as conn:
            committed = self._committed_e4(conn)
            if committed + amount > _e4(limit_krw):
                return limit_krw - _from_e4(committed)
            conn.execute(
                self._entry.insert().values(
                    id=r.id,
                    budget_id=self.budget_id,
                    model=r.model[:200],
                    purpose=r.purpose[:200],
                    reserved_e4=amount,
                    cost_e4=None,
                    status="open",
                    created_at=self._ts(r.created_at),
                    expires_at=self._ts(r.expires_at),
                    owner=self.owner[:200],
                    note="",
                )
            )
            return None

    def settle(self, reservation_id: str, cost_krw: Decimal, *, note: str, now: datetime) -> None:
        e = self._entry
        with self._tx() as conn:
            res = conn.execute(
                e.update()
                .where(
                    e.c.id == reservation_id,
                    e.c.budget_id == self.budget_id,
                    e.c.status.in_(("open", "reconciled")),
                )
                .values(
                    status="settled", cost_e4=_e4(cost_krw), settled_at=self._ts(now), note=note
                )
            )
            if res.rowcount != 1:
                raise RuntimeError(f"reservation {reservation_id} unknown or already settled")

    def totals(self, *, now: datetime) -> BudgetTotals:
        sa, e = self._sa, self._entry
        with self.engine.connect() as conn:
            rows = conn.execute(
                sa.select(e.c.id, e.c.reserved_e4, e.c.cost_e4, e.c.status, e.c.expires_at).where(
                    e.c.budget_id == self.budget_id
                )
            ).all()
        entries = {
            r.id: _Entry(
                r.id,
                _from_e4(r.reserved_e4),
                None if r.cost_e4 is None else _from_e4(r.cost_e4),
                r.status,
                r.expires_at if r.expires_at.tzinfo else r.expires_at.replace(tzinfo=UTC),
            )
            for r in rows
        }
        return _totals(entries, Decimal(0), now)

    def reconcile_expired(self, *, now: datetime, note: str) -> list[str]:
        sa, e = self._sa, self._entry
        with self._tx() as conn:
            cond = (
                (e.c.budget_id == self.budget_id)
                & (e.c.status == "open")
                & (e.c.expires_at <= self._ts(now))
            )
            ids = [r.id for r in conn.execute(sa.select(e.c.id).where(cond)).all()]
            if ids:
                conn.execute(
                    e.update()
                    .where(cond)
                    .values(
                        status="reconciled",
                        cost_e4=e.c.reserved_e4,
                        settled_at=self._ts(now),
                        note=note,
                    )
                )
            return ids
