"""Repository of Agent investigations (table ``agent_investigations``).

Every query filters by ``tenant_id``; an id of another tenant behaves like a missing id.

The investigation row and its ``investigate_decision`` job are created in ONE write
transaction. The job row is written here instead of through ``JobQueue.enqueue`` on purpose:
``enqueue`` only accepts the generic job types of ``POST /jobs``, and an investigation job
without its investigation row (or the other way round) must not exist.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from jettae.db.jobs import JobStatus
from jettae.db.orm import JobRow
from jettae.db.orm_investigations import InvestigationRow
from jettae.db.session import Database
from jettae.domain.dates import utc_now

JOB_TYPE = "investigate_decision"
STATUSES = ("queued", "running", "succeeded", "failed", "refused")
ACTIVE = frozenset({"queued", "running"})
# A retried live job would pay for the model calls again; one retry covers a lost worker.
MAX_JOB_ATTEMPTS = 2


@dataclass(frozen=True)
class InvestigationView:
    id: str
    tenant_id: str
    decision_id: str
    strategy: str
    mode: str
    status: str
    job_id: str | None
    requested_by: str
    decision_result_hash: str | None
    findings: list[dict[str, Any]]
    usage: dict[str, Any] | None
    report: dict[str, Any] | None
    error: Any
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _view(r: InvestigationRow, job: JobRow | None = None) -> InvestigationView:
    status, error = r.status, r.error
    # A cancelled job, or one that failed outside the handler (e.g. lease lost on the last
    # attempt), never reaches the handler's final write: the row would stay queued/running
    # forever. The job is the source of truth for that case.
    if (
        status in ACTIVE
        and job is not None
        and job.status
        in (
            JobStatus.CANCELLED.value,
            JobStatus.FAILED.value,
        )
    ):
        status = "failed"
        error = {"code": f"job_{job.status}", "message": "조사 작업이 끝나지 못했습니다."}
    return InvestigationView(
        id=r.id,
        tenant_id=r.tenant_id,
        decision_id=r.decision_id,
        strategy=r.strategy,
        mode=r.mode,
        status=status,
        job_id=r.job_id,
        requested_by=r.requested_by,
        decision_result_hash=r.decision_result_hash,
        findings=list(r.findings or []),
        usage=dict(r.usage) if r.usage is not None else None,
        report=dict(r.report) if r.report is not None else None,
        error=error,
        created_at=r.created_at,
        started_at=r.started_at,
        finished_at=r.finished_at,
    )


class SqlInvestigations:
    def __init__(
        self,
        db: Database,
        *,
        clock: Callable[[], datetime] = utc_now,
        max_attempts: int = MAX_JOB_ATTEMPTS,
    ) -> None:
        self.db = db
        self.clock = clock
        self.max_attempts = max(1, max_attempts)

    # ------------------------------------------------------------------ producer side
    def create(
        self,
        tenant_id: str,
        *,
        decision_id: str,
        strategy: str,
        mode: str,
        requested_by: str,
        decision_result_hash: str | None,
    ) -> InvestigationView:
        now = self.clock()
        inv_id = f"inv_{uuid.uuid4().hex}"
        job_id = f"job_{uuid.uuid4().hex}"
        with self.db.write(tenant_id) as s:
            s.add(
                JobRow(
                    id=job_id,
                    tenant_id=tenant_id,
                    type=JOB_TYPE,
                    status=JobStatus.QUEUED.value,
                    payload={"investigation_id": inv_id},
                    attempts=0,
                    max_attempts=self.max_attempts,
                    run_after=now,
                    lease_version=0,
                    cancel_requested=False,
                    created_by=requested_by,
                    created_at=now,
                    updated_at=now,
                )
            )
            row = InvestigationRow(
                tenant_id=tenant_id,
                id=inv_id,
                decision_id=decision_id,
                strategy=strategy,
                mode=mode,
                status="queued",
                job_id=job_id,
                requested_by=requested_by,
                decision_result_hash=decision_result_hash,
                findings=[],
                created_at=now,
                updated_at=now,
            )
            s.add(row)
            s.flush()
            return _view(row)

    # ------------------------------------------------------------------ reads
    def get(self, tenant_id: str, investigation_id: str) -> InvestigationView | None:
        with self.db.session() as s:
            r = s.get(InvestigationRow, (tenant_id, investigation_id), populate_existing=True)
            if r is None:
                return None
            return _view(r, self._job(s, r))

    def list_for_decision(
        self, tenant_id: str, decision_id: str, *, limit: int = 20
    ) -> list[InvestigationView]:
        """Newest first."""
        with self.db.session() as s:
            q = (
                sa.select(InvestigationRow, JobRow)
                .outerjoin(
                    JobRow,
                    sa.and_(
                        JobRow.id == InvestigationRow.job_id,
                        JobRow.tenant_id == InvestigationRow.tenant_id,
                    ),
                )
                .where(
                    InvestigationRow.tenant_id == tenant_id,
                    InvestigationRow.decision_id == decision_id,
                )
                .order_by(InvestigationRow.created_at.desc(), InvestigationRow.id.desc())
                .limit(limit)
            )
            return [_view(r, j) for r, j in s.execute(q).all()]

    @staticmethod
    def _job(s: Any, r: InvestigationRow) -> JobRow | None:
        if r.job_id is None:
            return None
        j = s.get(JobRow, r.job_id, populate_existing=True)
        return j if j is not None and j.tenant_id == r.tenant_id else None

    # ------------------------------------------------------------------ worker side
    def mark_running(
        self, tenant_id: str, investigation_id: str, *, decision_result_hash: str | None
    ) -> None:
        now = self.clock()
        with self.db.write(tenant_id) as s:
            r = s.get(InvestigationRow, (tenant_id, investigation_id), with_for_update=True)
            if r is None:
                return
            r.status = "running"
            r.started_at = r.started_at or now
            r.decision_result_hash = decision_result_hash or r.decision_result_hash
            r.updated_at = now

    def finish(
        self,
        tenant_id: str,
        investigation_id: str,
        *,
        status: str,
        findings: Sequence[Mapping[str, Any]],
        usage: Mapping[str, Any] | None,
        report: Mapping[str, Any] | None,
        error: Any,
    ) -> None:
        """Final state. Joins the caller's write transaction (the worker commits it together
        with the job's ``succeeded`` mark)."""
        if status not in STATUSES or status in ACTIVE:
            raise ValueError(f"not a final investigation status: {status!r}")
        now = self.clock()
        with self.db.write(tenant_id) as s:
            r = s.get(InvestigationRow, (tenant_id, investigation_id), with_for_update=True)
            if r is None:
                return
            r.status = status
            r.findings = [dict(f) for f in findings]
            r.usage = dict(usage) if usage is not None else None
            r.report = dict(report) if report is not None else None
            r.error = error
            r.started_at = r.started_at or now
            r.finished_at = now
            r.updated_at = now
            s.flush()
