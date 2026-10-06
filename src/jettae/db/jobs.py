"""DB-backed job queue with leases, heartbeats, retry/backoff and crash recovery.

State machine::

    queued --claim--> running --complete--> succeeded
       ^                 |  \\--fail(permanent | attempts exhausted)--> failed
       |                 |   \\--cancel requested--> cancelled
       +--fail(transient, attempts left; run_after = now + backoff)
    queued --cancel--> cancelled
    running (lease expired = worker crashed) --claim--> running (attempt + 1)

Claiming is safe under concurrency: on PostgreSQL candidates are locked with
``FOR UPDATE SKIP LOCKED``; on SQLite the claim runs in a ``BEGIN IMMEDIATE`` transaction.
Every lease carries ``lease_version``; heartbeat / complete / fail only succeed while the
row still holds the same owner and version, so a worker that lost its lease can never
overwrite the result of the worker that took over.
"""

from __future__ import annotations

import builtins
import random
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

import sqlalchemy as sa

from jettae.db.orm import JobRow
from jettae.db.session import Database
from jettae.domain.dates import utc_now


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})
# Types accepted by the generic POST /jobs. ``investigate_decision`` is deliberately absent: it is
# enqueued only together with its agent_investigations row (db/repos_investigations.py).
JOB_TYPES = frozenset({"ingest_document", "run_analysis", "apply_change"})


class LeaseLostError(RuntimeError):
    """The worker no longer owns the job (lease expired and was taken over)."""


class JobCancelledError(RuntimeError):
    """Cancellation was requested while the job was running."""


@dataclass(frozen=True)
class JobView:
    id: str
    tenant_id: str
    type: str
    status: JobStatus
    payload: Mapping[str, Any]
    result: Any
    error: Any
    attempts: int
    max_attempts: int
    run_after: datetime
    cancel_requested: bool
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class Lease:
    job_id: str
    tenant_id: str
    type: str
    payload: Mapping[str, Any]
    attempt: int
    max_attempts: int
    owner: str
    version: int


@dataclass(frozen=True)
class Heartbeat:
    alive: bool
    cancel_requested: bool


def _view(r: JobRow) -> JobView:
    return JobView(
        id=r.id,
        tenant_id=r.tenant_id,
        type=r.type,
        status=JobStatus(r.status),
        payload=r.payload,
        result=r.result,
        error=r.error,
        attempts=r.attempts,
        max_attempts=r.max_attempts,
        run_after=r.run_after,
        cancel_requested=r.cancel_requested,
        created_by=r.created_by,
        created_at=r.created_at,
        updated_at=r.updated_at,
        started_at=r.started_at,
        finished_at=r.finished_at,
    )


class JobQueue:
    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], datetime] = utc_now,
        lease_s: float = 60.0,
        max_attempts: int = 5,
        backoff_base_s: float = 2.0,
        backoff_max_s: float = 300.0,
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self.lease_s = lease_s
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.rng = rng or random.Random()

    # ------------------------------------------------------------------ producer side
    def enqueue(
        self,
        tenant_id: str,
        type: str,
        payload: Mapping[str, Any],
        *,
        created_by: str | None = None,
        max_attempts: int | None = None,
        run_after: datetime | None = None,
    ) -> JobView:
        if type not in JOB_TYPES:
            raise ValueError(f"unknown job type {type!r}")
        now = self.clock()
        row = JobRow(
            id=f"job_{uuid.uuid4().hex}",
            tenant_id=tenant_id,
            type=type,
            status=JobStatus.QUEUED.value,
            payload=dict(payload),
            attempts=0,
            max_attempts=max_attempts or self.max_attempts,
            run_after=run_after or now,
            lease_version=0,
            cancel_requested=False,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        with self.db.session() as s:
            s.add(row)
            s.flush()
            return _view(row)

    def get(self, tenant_id: str, job_id: str) -> JobView | None:
        with self.db.session() as s:
            r = s.get(JobRow, job_id, populate_existing=True)
            if r is None or r.tenant_id != tenant_id:
                return None
            return _view(r)

    def list(
        self,
        tenant_id: str,
        *,
        after: tuple[datetime, str] | None = None,
        limit: int = 50,
        status: JobStatus | None = None,
    ) -> list[JobView]:
        """Newest first; cursor = (created_at, id) of the last item of the previous page."""
        with self.db.session() as s:
            q = sa.select(JobRow).where(JobRow.tenant_id == tenant_id)
            if status is not None:
                q = q.where(JobRow.status == status.value)
            if after is not None:
                ts, jid = after
                q = q.where(
                    sa.or_(
                        JobRow.created_at < ts, sa.and_(JobRow.created_at == ts, JobRow.id < jid)
                    )
                )
            q = q.order_by(JobRow.created_at.desc(), JobRow.id.desc()).limit(limit)
            return [_view(r) for r in s.scalars(q)]

    def cancel(self, tenant_id: str, job_id: str) -> JobView | None:
        """Queued jobs are cancelled immediately; running jobs get ``cancel_requested`` and
        stop at their next checkpoint (their transaction is rolled back). Terminal jobs are
        returned unchanged."""
        now = self.clock()
        with self.db.write(tenant_id) as s:
            r = s.get(JobRow, job_id, with_for_update=True, populate_existing=True)
            if r is None or r.tenant_id != tenant_id:
                return None
            if r.status == JobStatus.QUEUED.value:
                r.status = JobStatus.CANCELLED.value
                r.cancel_requested = True
                r.finished_at = now
            elif r.status == JobStatus.RUNNING.value:
                r.cancel_requested = True
            r.updated_at = now
            s.flush()
            return _view(r)

    # ------------------------------------------------------------------ worker side
    def claim(self, owner: str, limit: int = 1) -> builtins.list[Lease]:
        now = self.clock()
        leases: list[Lease] = []
        with self.db.write() as s:
            q = (
                sa.select(JobRow)
                .where(
                    sa.or_(
                        sa.and_(JobRow.status == JobStatus.QUEUED.value, JobRow.run_after <= now),
                        sa.and_(
                            JobRow.status == JobStatus.RUNNING.value,
                            JobRow.lease_expires_at < now,
                        ),
                    )
                )
                .order_by(JobRow.run_after, JobRow.created_at, JobRow.id)
                .limit(max(limit, 1) * 4)
                .execution_options(populate_existing=True)
            )
            if self.db.dialect == "postgresql":
                q = q.with_for_update(skip_locked=True)
            for r in s.scalars(q):
                if len(leases) >= limit:
                    break
                if r.status == JobStatus.RUNNING.value:  # previous worker crashed
                    if r.cancel_requested:
                        self._finish(r, JobStatus.CANCELLED, now, error=_err("cancelled", ""))
                        continue
                    if r.attempts >= r.max_attempts:
                        self._finish(
                            r,
                            JobStatus.FAILED,
                            now,
                            error=_err(
                                "lease_expired",
                                f"worker lease expired; {r.attempts} attempts used",
                            ),
                        )
                        continue
                r.status = JobStatus.RUNNING.value
                r.attempts += 1
                r.lease_owner = owner
                r.lease_version += 1
                r.lease_expires_at = now + timedelta(seconds=self.lease_s)
                r.heartbeat_at = now
                r.started_at = r.started_at or now
                r.updated_at = now
                leases.append(
                    Lease(
                        r.id,
                        r.tenant_id,
                        r.type,
                        dict(r.payload),
                        r.attempts,
                        r.max_attempts,
                        owner,
                        r.lease_version,
                    )
                )
            s.flush()
        return leases

    def _owned(self, s: Any, lease: Lease) -> JobRow:
        r = s.get(JobRow, lease.job_id, with_for_update=True, populate_existing=True)
        if (
            r is None
            or r.status != JobStatus.RUNNING.value
            or r.lease_owner != lease.owner
            or r.lease_version != lease.version
        ):
            raise LeaseLostError(lease.job_id)
        return r

    def heartbeat(self, lease: Lease) -> Heartbeat:
        now = self.clock()
        with self.db.write() as s:
            try:
                r = self._owned(s, lease)
            except LeaseLostError:
                return Heartbeat(False, False)
            r.lease_expires_at = now + timedelta(seconds=self.lease_s)
            r.heartbeat_at = now
            r.updated_at = now
            return Heartbeat(True, bool(r.cancel_requested))

    def complete(self, lease: Lease, result: Any) -> None:
        """Mark succeeded. Joins the ambient transaction when called inside
        ``db.write(...)`` so the job's work and its completion commit atomically.
        Raises LeaseLostError / JobCancelledError (the caller's transaction must roll back)."""
        now = self.clock()
        with self.db.write(lease.tenant_id) as s:
            r = self._owned(s, lease)
            if r.cancel_requested:
                raise JobCancelledError(lease.job_id)
            self._finish(r, JobStatus.SUCCEEDED, now, result=result)
            s.flush()

    def fail(self, lease: Lease, error: Mapping[str, Any], *, transient: bool) -> JobStatus:
        now = self.clock()
        with self.db.write(lease.tenant_id) as s:
            try:
                r = self._owned(s, lease)
            except LeaseLostError:
                return JobStatus.RUNNING
            if r.cancel_requested:
                self._finish(r, JobStatus.CANCELLED, now, error=dict(error))
            elif transient and r.attempts < r.max_attempts:
                r.status = JobStatus.QUEUED.value
                r.error = {**error, "retry": True, "attempt": r.attempts}
                r.run_after = now + timedelta(seconds=self.backoff(r.attempts))
                r.lease_owner = None
                r.lease_expires_at = None
                r.updated_at = now
            else:
                self._finish(r, JobStatus.FAILED, now, error={**error, "attempt": r.attempts})
            s.flush()
            return JobStatus(r.status)

    def mark_cancelled(self, lease: Lease) -> None:
        now = self.clock()
        with self.db.write(lease.tenant_id) as s:
            try:
                r = self._owned(s, lease)
            except LeaseLostError:
                return
            self._finish(r, JobStatus.CANCELLED, now, error=_err("cancelled", "취소 요청됨"))

    def backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter: base * 2^(attempt-1), capped, scaled by
        a random factor in [0.5, 1.0]."""
        raw = min(self.backoff_max_s, self.backoff_base_s * (2 ** max(attempt - 1, 0)))
        return raw * (0.5 + 0.5 * self.rng.random())

    @staticmethod
    def _finish(
        r: JobRow,
        status: JobStatus,
        now: datetime,
        *,
        result: Any = None,
        error: Any = None,
    ) -> None:
        r.status = status.value
        if result is not None:
            r.result = result
        if error is not None:
            r.error = error
        elif status is JobStatus.SUCCEEDED:
            r.error = None
        r.finished_at = now
        r.lease_owner = None
        r.lease_expires_at = None
        r.updated_at = now


def _err(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}
