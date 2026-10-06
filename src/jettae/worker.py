"""Job worker: claims jobs from the DB queue and runs them through ``app.services``.

* bounded concurrency (thread pool; ``--concurrency``), one lease per job;
* heartbeats extend the lease every ``lease/3`` seconds and pick up cancel requests;
* a job's database work and its ``succeeded`` mark commit in ONE transaction
  (``JobContext.transaction``), so a crash either leaves no trace or a finished job;
  after a crash the lease expires and another worker resumes the job (attempt + 1);
* transient errors (DB locked/disconnected, timeouts) are retried with exponential backoff;
  everything else fails the job permanently with a readable error.

CLI: ``jettae worker run [--once] [--concurrency N]`` (validates JETTAE_ENV and the
production settings before connecting).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Any

import sqlalchemy.exc as sa_exc
import typer
from pydantic import ValidationError

from jettae.app.contracts import (
    MAX_ISSUES_STORED,
    ApplyOutcome,
    IngestJobResult,
    MappingContractError,
    MappingRequest,
    MappingSource,
    ParseResult,
    mapping_error_text,
)
from jettae.app.dto import ChangeOutcome
from jettae.config import require_valid_environment
from jettae.db.ingest_bridge import IngestContractError, IngestUnavailable
from jettae.db.jobs import JobCancelledError, JobQueue, Lease, LeaseLostError
from jettae.db.plain import changes_from_plain, to_plain
from jettae.db.runtime import Runtime
from jettae.domain.dates import to_display
from jettae.domain.errors import JettaeError
from jettae.domain.models import DocumentVersion
from jettae.domain.money import RoundingMode
from jettae.domain.status import DocumentStatus
from jettae.evidence.snapshot import AnalysisConfig

log = logging.getLogger("jettae.worker")


class TransientJobError(RuntimeError):
    """Raise from a handler to request a retry with backoff."""


class PermanentJobError(RuntimeError):
    """Raise from a handler to fail the job without retry."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


TRANSIENT = (
    TransientJobError,
    TimeoutError,
    ConnectionError,
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
)


def is_transient(e: BaseException) -> bool:
    if isinstance(e, sa_exc.DBAPIError) and getattr(e, "connection_invalidated", False):
        return True
    return isinstance(e, TRANSIENT)


def error_info(e: BaseException) -> dict[str, Any]:
    code = getattr(e, "code", None)
    if not isinstance(code, str):
        code = "transient_error" if is_transient(e) else "job_error"
    msg = str(e) or type(e).__name__
    return {"code": code, "type": type(e).__name__, "message": msg[:2000]}


# --------------------------------------------------------------------------- context
@dataclass
class _TxResult:
    result: Any = None


@dataclass
class JobContext:
    rt: Runtime
    lease: Lease
    queue: JobQueue
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lost_event: threading.Event = field(default_factory=threading.Event)
    completed: bool = False

    @property
    def tenant_id(self) -> str:
        return self.lease.tenant_id

    @property
    def payload(self) -> Mapping[str, Any]:
        return self.lease.payload

    def check(self) -> None:
        """Cooperative checkpoint: stop if cancelled or the lease was lost."""
        if self.lost_event.is_set():
            raise LeaseLostError(self.lease.job_id)
        if self.cancel_event.is_set():
            raise JobCancelledError(self.lease.job_id)

    @contextmanager
    def transaction(self) -> Iterator[_TxResult]:
        """Tenant write transaction that also marks the job succeeded on exit (atomic)."""
        self.check()
        holder = _TxResult()
        with self.rt.db.write(self.tenant_id):
            yield holder
            self.queue.complete(self.lease, holder.result)  # raises -> rollback
        self.completed = True


Handler = Callable[[JobContext], Any]


# --------------------------------------------------------------------------- handlers
def outcome_to_plain(out: ChangeOutcome) -> dict[str, Any]:
    p = out.plan
    return {
        "plan": {
            "affected": sorted(p.affected),
            "reasons": {k: list(v) for k, v in sorted(p.reasons.items())},
            "fallback_full": p.fallback_full,
            "removed": sorted(p.removed),
            "notes": list(p.notes),
            "summary": p.reason,
        },
        "recomputed_groups": sorted(out.recomputed_groups),
        "changed": list(out.changed),
        "review_required": list(out.review_required),
        "removed": list(out.removed),
        "snapshot_hash": out.snapshot_hash,
    }


@dataclass(frozen=True)
class _MappingChoice:
    mapping: MappingRequest | None
    source: MappingSource
    # set when an earlier version's confirmed mapping exists but cannot be carried over:
    # the version must not be auto-mapped against the user's decision (-> NEEDS_MAPPING)
    blocked: str | None = None


Layout = tuple[str, tuple[str, ...]]  # (table name, header row)


def _layout(summary: Mapping[str, Any], table: str | None) -> list[Layout]:
    """(table, header row) pairs of a ``suggest_mapping`` summary (one table if named)."""
    out = []
    for t in summary.get("tables") or ():
        if not isinstance(t, Mapping):
            continue
        name = str(t.get("table", ""))
        if table is None or name == table:
            out.append((name, tuple(str(h) for h in t.get("headers") or ())))
    return out


def _translate(req: MappingRequest, old: list[Layout], new: list[Layout]) -> MappingRequest | None:
    """Carry a confirmed mapping over to another version's table by *header text*: column
    indexes are re-resolved against the new header row, so a reordered column keeps its
    meaning. None when the layouts cannot be matched unambiguously (a header the mapping
    uses is missing or duplicated, the table set differs, or the mapping removes a column -
    ``null`` - while the header row changed: which new column the user would have removed
    is then unknown)."""
    if not old or [n for n, _ in old] != [n for n, _ in new]:
        return None
    if old == new:
        return req
    if len(old) != 1 or any(idx is None for idx in req.columns.values()):
        return None
    (_, old_headers), (_, new_headers) = old[0], new[0]
    columns: dict[str, int | None] = {}
    for field_name, idx in req.columns.items():
        assert idx is not None
        if idx >= len(old_headers):
            return None
        header = old_headers[idx]
        hits = [i for i, h in enumerate(new_headers) if h == header]
        if not header.strip() or len(hits) != 1:
            return None
        columns[field_name] = hits[0]
    return req.model_copy(update={"columns": columns})


def _mapping_for(ctx: JobContext, doc: DocumentVersion, content: bytes) -> _MappingChoice:
    """Which column mapping to parse with.

    1. the mapping sent with the job; 2. the last mapping a user confirmed for this
    version; 3. otherwise the last mapping a user confirmed for an *earlier* version of the
    same document - a correction (v2) must not be auto-mapped again against the user's
    decision (e.g. a column the user deliberately removed as base date). It is reused only
    if it can be resolved against this version's header row by header text; if not, the
    version is NOT auto-mapped (``blocked``): the user confirms the mapping again."""
    rt, tenant = ctx.rt, ctx.tenant_id
    raw = ctx.payload.get("mapping")
    try:
        if raw is not None:
            return _MappingChoice(MappingRequest.parse(raw), MappingSource(kind="job"))
        confirmed = rt.mappings.latest(tenant, doc.id)
        if confirmed:
            return _MappingChoice(
                MappingRequest.from_stored(confirmed["mapping"]),
                MappingSource(
                    kind="confirmed",
                    doc_version_id=doc.id,
                    version=doc.version,
                    mapping_id=str(confirmed["id"]),
                ),
            )
        versions = rt.repos.documents.versions(tenant, doc.document_id)
        earlier = sorted(
            (v for v in versions if v.version < doc.version),
            key=lambda v: v.version,
            reverse=True,
        )
        for prev in earlier:
            prev_confirmed = rt.mappings.latest(tenant, prev.id)
            if not prev_confirmed:
                continue
            req = MappingRequest.from_stored(prev_confirmed["mapping"])
            mid = str(prev_confirmed["id"])
            prev_content = rt.blobs.get(tenant, prev.storage_key or "")
            translated = None
            if prev_content is not None:
                old = _layout(rt.ingest.suggest_mapping(prev, prev_content), req.table)
                new = _layout(rt.ingest.suggest_mapping(doc, content), req.table)
                translated = _translate(req, old, new)
            if translated is not None:
                return _MappingChoice(
                    translated,
                    MappingSource(
                        kind="inherited",
                        doc_version_id=prev.id,
                        version=prev.version,
                        mapping_id=mid,
                    ),
                )
            why = (
                f"v{prev.version}에서 확정한 열 매핑을 이 버전의 머리글에 맞출 수 없어 자동으로 "
                "읽지 않았습니다. 열 매핑을 다시 확인하세요."
            )
            return _MappingChoice(
                None,
                MappingSource(
                    kind="none",
                    doc_version_id=prev.id,
                    version=prev.version,
                    mapping_id=mid,
                    not_inherited=why,
                ),
                why,
            )
        return _MappingChoice(None, MappingSource(kind="none"))
    except (ValidationError, ValueError) as e:
        raise PermanentJobError("bad_mapping", mapping_error_text(e)) from None


def _needs_mapping(parsed: ParseResult, why: str) -> ParseResult:
    """``parsed`` turned into a NEEDS_MAPPING result (no records): used when automatic
    recognition would contradict a mapping the user confirmed for an earlier version."""
    if parsed.status is not DocumentStatus.PARSED:
        return parsed.model_copy(update={"notes": (*parsed.notes, why)})
    return ParseResult(
        status=DocumentStatus.NEEDS_MAPPING,
        text=parsed.text,
        reason=why,
        suggestion=parsed.suggestion,
        notes=(*parsed.notes, why),
    )


def _document_detail(parsed: ParseResult, outcome: ApplyOutcome) -> dict[str, Any]:
    """What the document detail shows: why it was (not) applied and every row issue."""
    return {
        "reason": parsed.reason,
        "notes": list(parsed.notes),
        "suggestion": to_plain(parsed.suggestion) if parsed.suggestion is not None else None,
        "counts": parsed.counts.model_dump(mode="json") if parsed.counts else None,
        "issues": [i.model_dump(mode="json") for i in parsed.issues[:MAX_ISSUES_STORED]],
        "issues_total": len(parsed.issues),
        "totals": [t.model_dump(mode="json") for t in parsed.totals],
        "application": outcome.model_dump(mode="json"),
    }


def handle_ingest_document(ctx: JobContext) -> Any:
    """Parse one document version (outside the transaction: slow) and hand the result to
    :class:`jettae.app.doc_apply.DocumentApplier`, which decides - inside the job's write
    transaction - whether this version may become the current one and applies it. A job
    for an older version, a retried job and a job that finished late all obey the same
    rule, because the decision is re-made against the head read in that transaction."""
    rt, tenant = ctx.rt, ctx.tenant_id
    dvid = ctx.payload.get("doc_version_id")
    if not isinstance(dvid, str):
        raise PermanentJobError("bad_payload", "doc_version_id required")
    doc = rt.repos.documents.get(tenant, dvid)
    if doc is None or doc.storage_key is None:
        raise PermanentJobError("not_found", f"document version {dvid} not found")
    content = rt.blobs.get(tenant, doc.storage_key)
    if content is None:
        raise PermanentJobError("blob_missing", f"stored file for {dvid} is missing")
    try:
        choice = _mapping_for(ctx, doc, content)
        parsed = rt.ingest.parse(doc, content, choice.mapping)  # slow: outside the transaction
    except IngestUnavailable as e:
        raise PermanentJobError(e.code, str(e)) from None
    except (IngestContractError, MappingContractError) as e:
        raise PermanentJobError(getattr(e, "code", "bad_mapping"), str(e)) from None
    if choice.blocked:
        parsed = _needs_mapping(parsed, choice.blocked)
    ctx.check()
    with ctx.transaction() as tx:
        report = rt.applier.apply(tenant, doc, parsed)
        outcome = report.outcome
        rt.documents.set_status(
            tenant,
            dvid,
            parsed.status,
            {**_document_detail(parsed, outcome), "mapping": choice.source.model_dump(mode="json")},
            text=parsed.text,
            apply_state=outcome.state.value,
            issue_count=len(parsed.issues),
        )
        result = IngestJobResult(
            doc_version_id=dvid,
            document_id=doc.document_id,
            version=doc.version,
            document_status=parsed.status,
            reason=parsed.reason,
            facts=len(parsed.facts),
            records=len(parsed.records),
            counts=parsed.counts,
            issues=parsed.issues[:MAX_ISSUES_STORED],
            issues_total=len(parsed.issues),
            totals=parsed.totals,
            notes=parsed.notes,
            application=outcome,
            impact=outcome_to_plain(report.impact) if report.impact is not None else None,
            mapping=choice.source,
        )
        tx.result = result.model_dump(mode="json")
    return tx.result


def _config_from_payload(p: Mapping[str, Any], default_as_of: date) -> AnalysisConfig:
    try:
        as_of = date.fromisoformat(p["as_of"]) if p.get("as_of") else default_as_of
        rounding = RoundingMode(p["rounding"]) if p.get("rounding") else RoundingMode.FLOOR
    except ValueError as e:
        raise PermanentJobError("bad_payload", str(e)) from None
    rollover = p.get("rollover")
    if rollover is not None and not isinstance(rollover, bool):
        raise PermanentJobError("bad_payload", "rollover must be true, false or null")
    return AnalysisConfig(as_of=as_of, rollover=rollover, rounding=rounding)


def handle_run_analysis(ctx: JobContext) -> Any:
    rt, tenant = ctx.rt, ctx.tenant_id
    cfg = _config_from_payload(ctx.payload, to_display(rt.clock()).date())
    with ctx.transaction() as tx:
        run = rt.service.run_analysis(tenant, config=cfg)
        by_status: dict[str, int] = {}
        by_review: dict[str, int] = {}
        for v in run.decisions:
            by_status[v.decision.status.value] = by_status.get(v.decision.status.value, 0) + 1
            by_review[v.review_status.value] = by_review.get(v.review_status.value, 0) + 1
        tx.result = {
            "snapshot_hash": run.snapshot_hash,
            "decisions": len(run.decisions),
            "by_status": dict(sorted(by_status.items())),
            "by_review_status": dict(sorted(by_review.items())),
            "unattributed_payments": [list(u) for u in run.unattributed],
        }
    return tx.result


def handle_apply_change(ctx: JobContext) -> Any:
    rt, tenant = ctx.rt, ctx.tenant_id
    try:
        changes = changes_from_plain(ctx.payload.get("changes"), tenant)
    except ValueError as e:
        raise PermanentJobError("bad_payload", str(e)) from None
    with ctx.transaction() as tx:
        tx.result = outcome_to_plain(rt.service.apply_change(tenant, changes))
    return tx.result


def handle_investigate_decision(ctx: JobContext, *, gateway_factory: Any = None) -> Any:
    """Agent investigation of one decision (``jettae.agents.investigations``).

    The agent run is slow (model calls) and read-only, so it runs outside the write
    transaction; only the final state of the investigation row is written, together with
    the job's ``succeeded`` mark. An investigation that ends ``failed`` / ``refused`` is
    still a finished job: the outcome is on the investigation row. Transient errors are
    retried while attempts are left; after that the row records the failure, so the page
    never shows a review that stays "running" forever."""
    from jettae.agents.investigations import InvestigationOutcome, investigate
    from jettae.db.repos_investigations import ACTIVE, SqlInvestigations

    rt, tenant = ctx.rt, ctx.tenant_id
    inv_id = ctx.payload.get("investigation_id")
    if not isinstance(inv_id, str):
        raise PermanentJobError("bad_payload", "investigation_id required")
    repo = SqlInvestigations(rt.db, clock=rt.clock)
    inv = repo.get(tenant, inv_id)
    if inv is None:
        raise PermanentJobError("not_found", f"investigation {inv_id} not found")
    if inv.status not in ACTIVE:
        return {"investigation_id": inv_id, "status": inv.status, "findings": len(inv.findings)}
    current = rt.repos.decisions.get_current(tenant, inv.decision_id)
    repo.mark_running(tenant, inv_id, decision_result_hash=current.result_hash if current else None)
    ctx.check()
    try:
        out = investigate(
            rt.service,
            tenant,
            inv.decision_id,
            strategy=inv.strategy,
            mode=inv.mode,
            gateway_factory=gateway_factory,
            actor=f"agent:{inv.requested_by}",
        )
    except (JobCancelledError, LeaseLostError):
        raise
    except Exception as e:
        if is_transient(e) and ctx.lease.attempt < ctx.lease.max_attempts:
            raise
        log.exception("investigation failed", extra={"job_id": ctx.lease.job_id})
        out = InvestigationOutcome.stopped("failed", "investigation_error", type(e).__name__)
    ctx.check()
    with ctx.transaction() as tx:
        repo.finish(
            tenant,
            inv_id,
            status=out.status,
            findings=out.findings,
            usage=out.usage,
            report=out.report,
            error=out.error,
        )
        tx.result = {
            "investigation_id": inv_id,
            "status": out.status,
            "findings": len(out.findings),
        }
    return tx.result


DEFAULT_HANDLERS: dict[str, Handler] = {
    "ingest_document": handle_ingest_document,
    "run_analysis": handle_run_analysis,
    "apply_change": handle_apply_change,
    "investigate_decision": handle_investigate_decision,
}


# --------------------------------------------------------------------------- worker
class _HeartbeatThread(threading.Thread):
    def __init__(self, queue: JobQueue, ctx: JobContext, interval: float) -> None:
        super().__init__(daemon=True, name=f"hb-{ctx.lease.job_id[:12]}")
        self.queue, self.ctx, self.interval = queue, ctx, interval
        self._stop_evt = threading.Event()

    def run(self) -> None:
        while not self._stop_evt.wait(self.interval):
            try:
                hb = self.queue.heartbeat(self.ctx.lease)
            except Exception:  # DB busy etc.: try again next tick
                log.warning("heartbeat failed", extra={"job_id": self.ctx.lease.job_id})
                continue
            if not hb.alive:
                self.ctx.lost_event.set()
                return
            if hb.cancel_requested:
                self.ctx.cancel_event.set()

    def stop(self) -> None:
        self._stop_evt.set()


class Worker:
    def __init__(
        self,
        rt: Runtime,
        *,
        owner: str | None = None,
        concurrency: int | None = None,
        poll_s: float | None = None,
        handlers: Mapping[str, Handler] | None = None,
        heartbeat_s: float | None = None,
    ) -> None:
        self.rt = rt
        self.queue = rt.queue
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.concurrency = max(1, concurrency or rt.settings.worker_concurrency)
        self.poll_s = poll_s if poll_s is not None else rt.settings.worker_poll_s
        self.handlers = dict(handlers or DEFAULT_HANDLERS)
        self.heartbeat_s = heartbeat_s or max(1.0, rt.queue.lease_s / 3)
        self.stop_event = threading.Event()

    def process(self, lease: Lease) -> str:
        """Run one leased job to a terminal (or retry) state. Returns the outcome label."""
        ctx = JobContext(self.rt, lease, self.queue)
        hb = _HeartbeatThread(self.queue, ctx, self.heartbeat_s)
        hb.start()
        extra = {"job_id": lease.job_id, "job_type": lease.type, "attempt": lease.attempt}
        try:
            handler = self.handlers.get(lease.type)
            if handler is None:
                raise PermanentJobError("unknown_job_type", f"no handler for {lease.type}")
            result = handler(ctx)
            if not ctx.completed:
                with self.rt.db.write(lease.tenant_id):
                    self.queue.complete(lease, result)
            log.info("job succeeded", extra=extra)
            return "succeeded"
        except JobCancelledError:
            self.queue.mark_cancelled(lease)
            log.info("job cancelled", extra=extra)
            return "cancelled"
        except LeaseLostError:
            log.warning("job lease lost; another worker owns it", extra=extra)
            return "lease_lost"
        except Exception as e:
            transient = is_transient(e) and not isinstance(e, PermanentJobError)
            status = self.queue.fail(lease, error_info(e), transient=transient)
            level = logging.WARNING if transient else logging.ERROR
            log.log(
                level,
                "job error",
                extra={**extra, "error": repr(e), "next_status": status.value},
                exc_info=not isinstance(e, PermanentJobError | JettaeError),
            )
            return f"error:{status.value}"
        finally:
            hb.stop()

    def run_once(self, *, max_rounds: int = 1000) -> int:
        """Process every job that is claimable now (incl. immediate retries); then return."""
        done = 0
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            for _ in range(max_rounds):
                leases = self.queue.claim(self.owner, limit=self.concurrency)
                if not leases:
                    break
                for f in [pool.submit(self.process, ls) for ls in leases]:
                    f.result()
                done += len(leases)
        return done

    def run_forever(self) -> None:
        inflight: set[Future[str]] = set()
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            while not self.stop_event.is_set():
                free = self.concurrency - len(inflight)
                if free > 0:
                    try:
                        for ls in self.queue.claim(self.owner, limit=free):
                            inflight.add(pool.submit(self.process, ls))
                    except Exception:
                        log.exception("claim failed")
                if inflight:
                    finished, _ = wait(inflight, timeout=self.poll_s, return_when=FIRST_COMPLETED)
                    inflight -= finished
                else:
                    self.stop_event.wait(self.poll_s)
            log.info("worker stopping; waiting for in-flight jobs", extra={"n": len(inflight)})
            wait(inflight)


# --------------------------------------------------------------------------- CLI
app = typer.Typer(help="DB 기반 작업 큐 worker", no_args_is_help=True)


@app.command("run")
def run_cmd(
    once: Annotated[bool, typer.Option("--once", help="처리 가능한 작업만 처리하고 종료")] = False,
    concurrency: Annotated[int | None, typer.Option("--concurrency", min=1)] = None,
    poll: Annotated[float | None, typer.Option("--poll", help="폴링 간격(초)")] = None,
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
) -> None:
    """작업(ingest_document, run_analysis, apply_change, investigate_decision)을 실행한다."""
    from jettae.db.logs import configure_logging

    configure_logging(log_level)
    # JETTAE_ENV, .env loading and the production settings are checked before connecting
    rt = Runtime.build(require_valid_environment("worker"))
    w = Worker(rt, concurrency=concurrency, poll_s=poll)
    if once:
        n = w.run_once()
        typer.echo(f"processed {n} job(s)")
        return

    def _stop(*_: Any) -> None:
        w.stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)
    log.info("worker started", extra={"owner": w.owner, "concurrency": w.concurrency})
    w.run_forever()
    rt.close()
