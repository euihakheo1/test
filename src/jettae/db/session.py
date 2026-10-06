"""Engine / session management with an ambient (context-local) session.

* ``Database.session()`` — reuse the ambient session if one is open in this context,
  otherwise open a short transaction (commit on success, rollback on error).
* ``Database.write(tenant_id)`` — a serialising write transaction for one tenant:
  SQLite: ``BEGIN IMMEDIATE`` (database-wide write lock, waits up to ``busy_timeout``);
  PostgreSQL: ``pg_advisory_xact_lock(<tenant key>)`` inside the transaction.
  Re-entrant: nested calls in the same context join the outer transaction.

Repositories always go through ``session()``, so a use case wrapped in ``write()`` (e.g. a
service call plus the job-completion update in the worker) commits atomically.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_SQLITE_BEGIN = "jettae_sqlite_begin"


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def tenant_lock_key(tenant_id: str) -> int:
    """Stable signed 64-bit key for PostgreSQL advisory locks."""
    return int.from_bytes(hashlib.sha256(tenant_id.encode()).digest()[:8], "big", signed=True)


def make_engine(url: str, *, echo: bool = False) -> Engine:
    u = sa.engine.make_url(url)
    kwargs: dict[str, Any] = {"echo": echo, "json_serializer": _json_dumps}
    if u.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if u.database in (None, "", ":memory:"):
            kwargs["poolclass"] = sa.pool.StaticPool
        else:
            Path(u.database).parent.mkdir(parents=True, exist_ok=True)
    else:
        kwargs["pool_pre_ping"] = True
    engine = sa.create_engine(u, **kwargs)
    if u.get_backend_name() == "sqlite":
        _install_sqlite_hooks(engine, in_memory=u.database in (None, "", ":memory:"))
    return engine


def _install_sqlite_hooks(engine: Engine, *, in_memory: bool) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _rec: Any) -> None:
        # take over transaction control from the sqlite3 module (SQLAlchemy recipe)
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        if not in_memory:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: sa.Connection) -> None:
        mode = conn.get_execution_options().get(_SQLITE_BEGIN, "DEFERRED")
        conn.exec_driver_sql(f"BEGIN {mode}")


class Database:
    def __init__(self, url: str, *, echo: bool = False, engine: Engine | None = None) -> None:
        self.url = url
        self.engine = engine or make_engine(url, echo=echo)
        self.dialect = self.engine.dialect.name
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=True)
        if self.dialect == "sqlite":
            self._write_engine = self.engine.execution_options(**{_SQLITE_BEGIN: "IMMEDIATE"})
            self._write_factory = sessionmaker(
                bind=self._write_engine, expire_on_commit=False, autoflush=True
            )
        else:
            self._write_factory = self._factory
        self._ambient: ContextVar[tuple[Session, bool] | None] = ContextVar(
            f"jettae_db_session_{id(self)}", default=None
        )

    @property
    def in_transaction(self) -> bool:
        return self._ambient.get() is not None

    @property
    def in_write_transaction(self) -> bool:
        cur = self._ambient.get()
        return cur is not None and cur[1]

    def current(self) -> Session | None:
        cur = self._ambient.get()
        return cur[0] if cur else None

    @contextmanager
    def _run(self, factory: sessionmaker[Session], write: bool) -> Iterator[Session]:
        s = factory()
        token = self._ambient.set((s, write))
        try:
            with s.begin():
                yield s
        finally:
            self._ambient.reset(token)
            s.close()

    @contextmanager
    def session(self) -> Iterator[Session]:
        cur = self._ambient.get()
        if cur is not None:
            yield cur[0]
            return
        with self._run(self._factory, write=False) as s:
            yield s

    @contextmanager
    def write(self, tenant_id: str | None = None) -> Iterator[Session]:
        cur = self._ambient.get()
        if cur is not None:
            if not cur[1]:
                raise RuntimeError("cannot upgrade a read transaction to a write transaction")
            yield cur[0]
            return
        with self._run(self._write_factory, write=True) as s:
            if self.dialect == "postgresql" and tenant_id is not None:
                s.execute(
                    sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": tenant_lock_key(tenant_id)}
                )
            yield s

    def dispose(self) -> None:
        self.engine.dispose()
