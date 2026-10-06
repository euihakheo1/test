"""Glue between HTTP routes and ``app.services`` / adapters. Routes only parse requests,
call one function here and serialise the result; this module holds no business rules
(those live in ``app.services`` and the pure packages)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from jettae.api.auth import Principal
from jettae.api.errors import ApiError
from jettae.api.reports import render_csv, render_html
from jettae.app.contracts import MappingContractError, MappingRequest, mapping_error_text
from jettae.app.dto import ChangeOutcome
from jettae.app.ports import DocumentHead
from jettae.db.jobs import JOB_TYPES, JobStatus, JobView
from jettae.db.plain import changes_from_plain, changes_to_payload, to_plain
from jettae.db.runtime import Runtime
from jettae.domain.errors import NotFoundError
from jettae.domain.hashing import bytes_hash
from jettae.domain.models import Decision, DocumentVersion, EvidenceLink
from jettae.domain.status import DocKind, ReconcileStatus, ReviewStatus

API = "/api/v1"


def head_out(h: DocumentHead | None) -> dict[str, Any] | None:
    if h is None:
        return None
    return {
        "document_id": h.document_id,
        "doc_version_id": h.doc_version_id,
        "version": h.version,
        "state": h.state,
        "fingerprint": h.fingerprint,
        "needs_ack": h.needs_ack,
        "acknowledged": h.acknowledged,
        "applied_at": h.applied_at.isoformat(),
        "ack_by": h.ack_by,
        "ack_at": h.ack_at.isoformat() if h.ack_at else None,
    }


def doc_out(
    d: DocumentVersion,
    detail: Any = None,
    *,
    apply: tuple[str | None, int | None] | None = None,
    head: DocumentHead | None = None,
) -> dict[str, Any]:
    state, issues = apply if apply is not None else (None, None)
    return {
        "id": d.id,
        "document_id": d.document_id,
        "version": d.version,
        "filename": d.filename,
        "media_type": d.media_type,
        "kind": d.kind.value,
        "size": d.size,
        "content_hash": d.content_hash,
        "status": d.status.value,
        "supersedes": d.supersedes,
        "created_at": d.created_at.isoformat(),
        "status_detail": detail,
        "apply_state": state,
        "issue_count": issues,
        "is_current": head is not None and head.doc_version_id == d.id,
        "current_version": head.version if head is not None else None,
    }


def job_out(j: JobView) -> dict[str, Any]:
    def ts(v: Any) -> Any:
        return v.isoformat() if v is not None else None

    return {
        "id": j.id,
        "type": j.type,
        "status": j.status.value,
        "attempts": j.attempts,
        "max_attempts": j.max_attempts,
        "payload": dict(j.payload),
        "result": j.result,
        "error": j.error,
        "cancel_requested": j.cancel_requested,
        "created_by": j.created_by,
        "created_at": ts(j.created_at),
        "updated_at": ts(j.updated_at),
        "run_after": ts(j.run_after),
        "started_at": ts(j.started_at),
        "finished_at": ts(j.finished_at),
    }


def accepted(j: JobView) -> dict[str, Any]:
    return {"job_id": j.id, "status": j.status.value, "status_url": f"{API}/jobs/{j.id}"}


class Ops:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    # ------------------------------------------------------------------ documents
    def upload(
        self,
        p: Principal,
        *,
        filename: str,
        media_type: str,
        content: bytes,
        kind: str,
        document_id: str | None,
        auto_ingest: bool,
    ) -> tuple[int, dict[str, Any]]:
        rt, t = self.rt, p.tenant_id
        try:
            dk = DocKind(kind)
        except ValueError:
            raise ApiError(
                422, "invalid_kind", f"kind must be one of: {', '.join(k.value for k in DocKind)}"
            ) from None
        if document_id is not None:
            if not document_id or len(document_id) > 128:
                raise ApiError(422, "invalid_document_id", "document_id must be 1-128 chars")
            if rt.documents.latest(t, document_id) is None:
                raise ApiError(404, "not_found", "document not found")
        h = bytes_hash(content)
        try:
            # dedupe inside the tenant-serialised write transaction (no unique constraint)
            with rt.db.write(t):
                dup = self._dedupe_target(t, h, document_id)
                if dup is not None:
                    return self._duplicate(dup, document_id)
                doc = rt.service.register_document(
                    t,
                    filename=filename,
                    content=content,
                    media_type=media_type,
                    kind=dk,
                    document_id=document_id,
                )
                job = (
                    rt.queue.enqueue(
                        t, "ingest_document", {"doc_version_id": doc.id}, created_by=p.email
                    )
                    if auto_ingest
                    else None
                )
        except IntegrityError:
            dup = self._dedupe_target(t, h, document_id)
            if dup is None:
                raise
            return self._duplicate(dup, document_id)
        return 201, {
            "document": doc_out(doc),
            "duplicate": False,
            "job_id": job.id if job else None,
        }

    def _dedupe_target(
        self, tenant_id: str, content_hash: str, document_id: str | None
    ) -> DocumentVersion | None:
        """The stored version an upload is a duplicate of, or None (store a new version).

        - with ``document_id`` (correction): only the *latest* version of that document
          counts, so restoring earlier content (A -> B -> A) creates version 3; identical
          content stored under another document is reported (409 by ``_duplicate``);
        - without: any version in the tenant with the same content."""
        docs = self.rt.documents
        if document_id is not None:
            latest = docs.latest(tenant_id, document_id)
            if latest is not None and latest.content_hash == content_hash:
                return latest
            other = docs.find_by_hash(tenant_id, content_hash)
            if other is not None and other.document_id != document_id:
                return other
            return None
        return docs.find_by_hash(tenant_id, content_hash)

    @staticmethod
    def _duplicate(dup: DocumentVersion, document_id: str | None) -> tuple[int, dict[str, Any]]:
        if document_id is not None and dup.document_id != document_id:
            raise ApiError(
                409,
                "duplicate_content",
                "identical content is already stored as another document version",
                {"document_id": dup.document_id, "doc_version_id": dup.id},
            )
        return 200, {"document": doc_out(dup), "duplicate": True, "job_id": None}

    def _outs(self, tenant_id: str, docs: list[DocumentVersion]) -> list[dict[str, Any]]:
        states = self.rt.documents.apply_states(tenant_id, [d.id for d in docs])
        heads = self.rt.heads.get_many(tenant_id, sorted({d.document_id for d in docs}))
        return [doc_out(d, apply=states.get(d.id), head=heads.get(d.document_id)) for d in docs]

    def list_documents(self, p: Principal, after: str | None, limit: int) -> dict[str, Any]:
        with self.rt.db.session():
            docs = self.rt.documents.page_latest(p.tenant_id, after=after, limit=limit + 1)
            more = len(docs) > limit
            docs = docs[:limit]
            outs = self._outs(p.tenant_id, docs)
        return {
            "items": [{"document_id": o["document_id"], "latest": o} for o in outs],
            "next_cursor": docs[-1].document_id if more and docs else None,
        }

    def versions(self, p: Principal, document_id: str) -> dict[str, Any]:
        with self.rt.db.session():
            vs = self.rt.documents.versions(p.tenant_id, document_id)
            if not vs:
                raise NotFoundError(document_id)
            head = self.rt.heads.get(p.tenant_id, document_id)
            items = self._outs(p.tenant_id, vs)
        return {"document_id": document_id, "items": items, "current": head_out(head)}

    def acknowledge(self, p: Principal, dvid: str, fingerprint: str) -> dict[str, Any]:
        """Record that the user reviewed the excluded rows / unread tables / total
        differences of the current version (unblocks approvals of decisions citing it) and
        remove the records carried over from earlier versions (see ``doc_apply`` rule 7)."""
        self._version(p, dvid)
        report = self.rt.applier.acknowledge_report(p.tenant_id, dvid, fingerprint, p.email)
        out = head_out(report.head)
        assert out is not None
        out["records_removed"] = len(report.removed)
        out["removed"] = list(report.removed[:200])
        return out

    def _version(self, p: Principal, dvid: str) -> DocumentVersion:
        d = self.rt.documents.get(p.tenant_id, dvid)
        if d is None:
            raise NotFoundError(dvid)
        return d

    def version(self, p: Principal, dvid: str) -> dict[str, Any]:
        with self.rt.db.session():
            d = self._version(p, dvid)
            states = self.rt.documents.apply_states(p.tenant_id, [dvid])
            return doc_out(
                d,
                self.rt.documents.status_detail(p.tenant_id, dvid),
                apply=states.get(dvid),
                head=self.rt.heads.get(p.tenant_id, d.document_id),
            )

    def content(self, p: Principal, dvid: str) -> tuple[bytes, DocumentVersion]:
        d = self._version(p, dvid)
        data = self.rt.blobs.get(p.tenant_id, d.storage_key or "")
        if data is None:
            raise ApiError(410, "content_missing", "stored file is not available")
        return data, d

    def spans(self, p: Principal, dvid: str, after: str | None, limit: int) -> dict[str, Any]:
        with self.rt.db.session():
            self._version(p, dvid)
            facts = [f for f in self.rt.facts.for_document(p.tenant_id, dvid) if f.span]
        if after is not None:
            facts = [f for f in facts if f.id > after]
        page, more = facts[:limit], len(facts) > limit
        return {
            "items": [
                {
                    "fact_id": f.id,
                    "kind": f.kind,
                    "subject_id": f.subject_id,
                    "value": to_plain(f.value),
                    "locator": to_plain(f.span.locator) if f.span else {},
                    "excerpt": f.span.excerpt if f.span else "",
                }
                for f in page
            ],
            "next_cursor": page[-1].id if more and page else None,
        }

    # ------------------------------------------------------------------ mapping
    def get_mapping(self, p: Principal, dvid: str) -> dict[str, Any]:
        d = self._version(p, dvid)
        detail = self.rt.documents.status_detail(p.tenant_id, dvid) or {}
        suggestion = detail.get("suggestion")
        if suggestion is None:
            content = self.rt.blobs.get(p.tenant_id, d.storage_key or "")
            if content is None:
                raise ApiError(410, "content_missing", "stored file is not available")
            suggestion = to_plain(self.rt.ingest.suggest_mapping(d, content))
        confirmed = self.rt.mappings.latest(p.tenant_id, dvid)
        if confirmed is not None:
            try:
                # stored before the split contract? present it in the current shape
                shaped: Any = MappingRequest.from_stored(confirmed["mapping"]).model_dump(
                    mode="json"
                )
                legacy = False
            except (ValidationError, ValueError):
                shaped, legacy = None, True
            confirmed = {
                **confirmed,
                "mapping": shaped,
                "legacy_unreadable": legacy,
                "confirmed_at": confirmed["confirmed_at"].isoformat(),
            }
        return {
            "doc_version_id": dvid,
            "document_status": d.status.value,
            "suggestion": suggestion,
            "confirmed": confirmed,
        }

    def confirm_mapping(
        self, p: Principal, dvid: str, mapping: MappingRequest, reingest: bool
    ) -> tuple[int, dict[str, Any]]:
        t = p.tenant_id
        mid = f"map_{uuid.uuid4().hex}"
        plain = mapping.model_dump(mode="json")
        with self.rt.db.write(t):
            self._version(p, dvid)
            self.rt.mappings.confirm(t, dvid, plain, p.email, mid)
            job = (
                self.rt.queue.enqueue(
                    t,
                    "ingest_document",
                    {"doc_version_id": dvid, "mapping": plain},
                    created_by=p.email,
                )
                if reingest
                else None
            )
        return 201, {"mapping_id": mid, "doc_version_id": dvid, "job_id": job.id if job else None}

    # ------------------------------------------------------------------ jobs
    def validate_job(self, p: Principal, type_: str, params: dict[str, Any]) -> dict[str, Any]:
        if type_ not in JOB_TYPES:
            raise ApiError(422, "invalid_job_type", f"unknown job type {type_}")
        if type_ == "apply_change":
            changes_from_plain(params.get("changes"), p.tenant_id)  # 422 on bad input
            return {"changes": params["changes"]}
        if type_ == "ingest_document":
            dvid = params.get("doc_version_id")
            if not isinstance(dvid, str):
                raise ApiError(422, "validation_error", "params.doc_version_id required")
            self._version(p, dvid)
            unknown_keys = set(params) - {"doc_version_id", "mapping"}
            if unknown_keys:
                raise ApiError(422, "validation_error", f"unknown params: {sorted(unknown_keys)}")
            out: dict[str, Any] = {"doc_version_id": dvid}
            if params.get("mapping") is not None:
                try:
                    req = MappingRequest.parse(params["mapping"])
                except (ValidationError, ValueError) as e:
                    raise MappingContractError(f"params.mapping: {mapping_error_text(e)}") from None
                out["mapping"] = req.model_dump(mode="json")
            return out
        allowed = {"as_of", "rollover", "rounding"}
        unknown = set(params) - allowed
        if unknown:
            raise ApiError(422, "validation_error", f"unknown params: {sorted(unknown)}")
        from datetime import date

        if params.get("as_of") is not None:
            try:
                date.fromisoformat(params["as_of"])
            except (TypeError, ValueError):
                raise ApiError(422, "validation_error", "as_of must be YYYY-MM-DD") from None
        if params.get("rounding") not in (None, "floor", "half_up"):
            raise ApiError(422, "validation_error", "rounding must be floor or half_up")
        if params.get("rollover") not in (None, True, False):
            raise ApiError(422, "validation_error", "rollover must be true, false or null")
        return dict(params)

    def create_job(self, p: Principal, type_: str, params: dict[str, Any]) -> tuple[int, Any]:
        payload = self.validate_job(p, type_, params)
        job = self.rt.queue.enqueue(p.tenant_id, type_, payload, created_by=p.email)
        return 202, accepted(job)

    def job(self, p: Principal, job_id: str) -> dict[str, Any]:
        j = self.rt.queue.get(p.tenant_id, job_id)
        if j is None:
            raise NotFoundError(job_id)
        return job_out(j)

    def list_jobs(
        self, p: Principal, cursor: Any, limit: int, status: str | None
    ) -> dict[str, Any]:
        from datetime import datetime

        after = None
        if cursor is not None:
            try:
                after = (datetime.fromisoformat(cursor[0]), str(cursor[1]))
            except (TypeError, ValueError, IndexError):
                raise ApiError(400, "invalid_cursor", "invalid pagination cursor") from None
        st = None
        if status is not None:
            try:
                st = JobStatus(status)
            except ValueError:
                raise ApiError(422, "validation_error", "unknown job status") from None
        jobs = self.rt.queue.list(p.tenant_id, after=after, limit=limit + 1, status=st)
        more = len(jobs) > limit
        jobs = jobs[:limit]
        nxt = [jobs[-1].created_at.isoformat(), jobs[-1].id] if more and jobs else None
        return {"items": [job_out(j) for j in jobs], "next_cursor": nxt}

    def cancel_job(self, p: Principal, job_id: str) -> dict[str, Any]:
        j = self.rt.queue.cancel(p.tenant_id, job_id)
        if j is None:
            raise NotFoundError(job_id)
        return job_out(j)

    # ------------------------------------------------------------------ changes
    def submit_changes(self, p: Principal, items: list[dict[str, Any]]) -> tuple[int, Any]:
        changes = changes_from_plain(items, p.tenant_id)  # validates; 422 on error
        job = self.rt.queue.enqueue(
            p.tenant_id,
            "apply_change",
            {"changes": changes_to_payload(changes)},
            created_by=p.email,
        )
        return 202, accepted(job)

    def upload_change(
        self,
        p: Principal,
        *,
        filename: str,
        media_type: str,
        content: bytes,
        kind: str,
        document_id: str | None,
    ) -> tuple[int, Any]:
        status, body = self.upload(
            p,
            filename=filename,
            media_type=media_type,
            content=content,
            kind=kind,
            document_id=document_id,
            auto_ingest=True,
        )
        if body["job_id"] is None:  # identical file already stored: nothing changes
            return 200, {**body, "status": "unchanged", "status_url": None}
        return 202, {
            **body,
            "status": JobStatus.QUEUED.value,
            "status_url": f"{API}/jobs/{body['job_id']}",
        }

    # ------------------------------------------------------------------ decisions
    def _summary(self, view: Any) -> dict[str, Any]:
        d: Decision = view.decision
        return {
            "id": d.id,
            "subject_id": d.subject_id,
            "status": d.status.value,
            "review_status": view.review_status.value,
            "result_hash": d.result_hash,
            "snapshot_hash": d.snapshot_hash,
            "required_documents": list(d.required_documents),
            "unresolved": list(d.unresolved),
            "missing": list(d.missing),
            **self._evidence_summary(d),
        }

    @staticmethod
    def _evidence_summary(d: Decision) -> dict[str, Any]:
        ev = d.computation("evidence")
        if ev is None:
            return {"basis": None, "basis_id": None, "counted": None}
        o = ev.outputs
        return {
            "basis": o.get("basis"),
            "basis_id": o.get("basis_id"),
            "counted": bool(o.get("counted", True)),
        }

    def list_decisions(
        self,
        p: Principal,
        *,
        after: str | None,
        limit: int,
        statuses: list[str],
        review_statuses: list[str],
        subject_id: str | None,
    ) -> dict[str, Any]:
        for s in statuses:
            if s not in ReconcileStatus.__members__:
                raise ApiError(422, "validation_error", f"unknown status {s}")
        for s in review_statuses:
            if s not in ReviewStatus.__members__:
                raise ApiError(422, "validation_error", f"unknown review_status {s}")
        t = p.tenant_id
        out: list[dict[str, Any]] = []
        cursor = after
        more = False
        with self.rt.db.session():
            while len(out) <= limit:
                ids = self.rt.decisions.page_ids(
                    t,
                    after=cursor,
                    limit=max(limit * 2, 20),
                    statuses=statuses,
                    subject_id=subject_id,
                )
                if not ids:
                    break
                for did in ids:
                    cursor = did
                    view = self.rt.service.decision_view(t, did)
                    if review_statuses and view.review_status.value not in review_statuses:
                        continue
                    out.append(self._summary(view))
                    if len(out) > limit:
                        more = True
                        break
                if more:
                    break
        page = out[:limit]
        return {"items": page, "next_cursor": page[-1]["id"] if more and page else None}

    def decision_detail(self, p: Principal, did: str) -> dict[str, Any]:
        t, rt = p.tenant_id, self.rt
        with rt.db.session():
            view = rt.service.decision_view(t, did)
            d = view.decision
            facts = []
            for fid in d.facts_used:
                f = rt.repos.facts.get(t, fid)
                if f is None:
                    continue
                facts.append(
                    {
                        "id": f.id,
                        "kind": f.kind,
                        "subject_id": f.subject_id,
                        "value": to_plain(f.value),
                        "extractor": f.extractor,
                        "observed_at": f.observed_at.isoformat(),
                        "span": to_plain(f.span) if f.span else None,
                    }
                )
            history = rt.repos.decisions.history(t, did)
        due = d.computation("due")
        variants: list[Any] = []
        if due is not None and not due.outputs.get("insufficient"):
            variants = to_plain(list(due.outputs.get("variants", ())))
        return {
            **self._summary(view),
            "facts": facts,
            "computations": [
                {
                    "name": c.name,
                    "rule_version": c.rule_version,
                    "inputs": to_plain(c.inputs),
                    "outputs": to_plain(c.outputs),
                }
                for c in d.computations
            ],
            "variants": variants,
            "allocations": to_plain(list(d.allocations)),
            "assumptions": list(d.assumptions),
            "rule_versions": list(d.rule_versions),
            "explanation": view.explanation,
            "checks": [
                {"name": c.name, "passed": c.passed, "details": list(c.details)}
                for c in view.checks
            ],
            "approvals": [self._approval(a, d) for a in view.approvals],
            "history": [
                {
                    "recorded_at": h.recorded_at.isoformat(),
                    "result_hash": h.decision.result_hash,
                    "status": h.decision.status.value,
                    "superseded": h.superseded,
                }
                for h in history
            ],
        }

    @staticmethod
    def _approval(a: Any, current: Decision | None) -> dict[str, Any]:
        return {
            "id": a.id,
            "decision_id": a.decision_id,
            "result_hash": a.result_hash,
            "snapshot_hash": a.snapshot_hash,
            "approved_by": a.approved_by,
            "approved_at": a.approved_at.isoformat(),
            "current": current is not None and a.result_hash == current.result_hash,
        }

    # ------------------------------------------------------------------ approvals
    def approve(self, p: Principal, did: str, expected: str) -> tuple[int, Any]:
        a = self.rt.service.approve(p.tenant_id, did, expected, p.email)
        cur = self.rt.repos.decisions.get_current(p.tenant_id, did)
        return 201, self._approval(a, cur)

    # ------------------------------------------------------------------ evidence links
    @staticmethod
    def _link_out(lk: EvidenceLink) -> dict[str, Any]:
        return {
            "id": lk.id,
            "invoice_id": lk.invoice_id,
            "relation": lk.relation.value,
            "settlement_line_id": lk.settlement_line_id,
            "document_entity": lk.document_entity,
            "confirmed_by": lk.confirmed_by,
            "note": lk.note,
        }

    @staticmethod
    def _link_outcome(link: dict[str, Any] | None, out: ChangeOutcome) -> dict[str, Any]:
        return {
            "link": link,
            "changed": list(out.changed),
            "review_required": list(out.review_required),
            "removed": list(out.removed),
            "snapshot_hash": out.snapshot_hash,
        }

    def confirm_link(self, p: Principal, did: str, body: dict[str, Any]) -> tuple[int, Any]:
        link, out = self.rt.service.confirm_evidence_link(
            p.tenant_id,
            did,
            body["expected_result_hash"],
            relation=body["relation"],
            settlement_line_id=body.get("settlement_line_id"),
            invoice_id=body.get("invoice_id"),
            note=body.get("note") or "",
            confirmed_by=p.email,  # from the credential, never from the body
        )
        return 200, self._link_outcome(self._link_out(link), out)

    def withdraw_link(self, p: Principal, link_id: str) -> dict[str, Any]:
        out = self.rt.service.withdraw_evidence_link(p.tenant_id, link_id)
        return self._link_outcome(None, out)

    def list_links(self, p: Principal) -> dict[str, Any]:
        with self.rt.db.session():
            links = self.rt.service.list_evidence_links(p.tenant_id)
        return {"items": [self._link_out(lk) for lk in links]}

    def approvals(self, p: Principal, did: str) -> dict[str, Any]:
        with self.rt.db.session():
            cur = self.rt.repos.decisions.get_current(p.tenant_id, did)
            if cur is None and not self.rt.repos.decisions.history(p.tenant_id, did):
                raise NotFoundError(did)
            items = self.rt.repos.approvals.list_for(p.tenant_id, did)
        return {"items": [self._approval(a, cur) for a in items]}

    # ------------------------------------------------------------------ reports
    def export(
        self,
        p: Principal,
        decision_ids: Iterable[str] | None,
        fmt: str,
        require_approved: bool,
    ) -> tuple[bytes, str, str]:
        t = p.tenant_id
        with self.rt.db.session():
            ids = list(decision_ids) if decision_ids else sorted(self.rt.repos.decisions.current(t))
            report = self.rt.service.export_report(t, ids, require_approved=require_approved)
        stamp = report.generated_at.strftime("%Y%m%dT%H%M%SZ")
        if fmt == "html":
            return render_html(report), "text/html; charset=utf-8", f"jettae-report-{stamp}.html"
        return render_csv(report), "text/csv; charset=utf-8", f"jettae-report-{stamp}.csv"
