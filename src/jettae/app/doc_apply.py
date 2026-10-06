"""Document application: the ONE place that decides how a parsed document version changes
the current ledger. The job worker, the in-process ingest path (``jettae.ingest.pipeline.
ingest_document``, used by the CLI/demo) and the acknowledgment API all go through
:class:`DocumentApplier`.

Rules (each is easy to misread, so they are spelled out):

1. **Version promotion.** Every document has one *current* version (:class:`DocumentHead`),
   the version whose rows are in the ledger. A parsed version ``v`` may become current only
   if no version newer than ``v`` is already current (``v >= head.version``). An older
   version - a retried job, a job that finished late, a user re-running v1 - is parsed and
   its summary kept on that version for inspection, but it never touches the ledger or the
   approvals. Re-applying the current version is allowed (e.g. after a new mapping); if its
   parse output is identical (same fingerprint) nothing is written.
2. **Atomicity.** The head is read, compared and written in the same tenant write
   transaction as the ledger changes (``UnitOfWork.transaction``: SQLite ``BEGIN
   IMMEDIATE``, PostgreSQL per-tenant advisory lock). Two appliers of the same document are
   therefore serialised and the second one sees the first one's head.
3. **Parse failure is not an empty revision.** A result that is not ``PARSED`` (needs
   mapping, corrupt, scanned, failed), a ``PARSED`` result without any recognised table,
   and a table whose every data row was excluded all leave the existing ledger untouched.
4. **Zero-row revision.** A recognised table with zero data rows and no row errors is a
   valid revision: the document's previous records are removed (the outcome reports how
   many) and the version becomes current with state ``applied_empty``.
5. **Ownership.** The records a document owns are found by provenance (facts whose source
   span points at any version of the document, and the records citing them), never by id
   conventions. A new current version replaces exactly that set: rows it still contains are
   UPDATEd, new rows ADDed, the rest REMOVEd.
6. **Partially applied documents.** Rows that could not be read are *excluded* (no record);
   cells that could not be read inside an applied row are *value* issues; a 합계 row that
   differs from the sum of the data rows is a *total mismatch*; a table with data rows
   whose format was not recognised is *unread* (``RowCounts.tables_unread``). Any of these
   makes the application ``applied_needs_ack``: the readable rows are current (so the
   analysis is not stale), but decisions citing the document - any version of it - cannot
   be approved until a user acknowledges this exact parse output
   (:meth:`DocumentApplier.acknowledge`). A new parse output (different fingerprint) needs
   a new acknowledgment.
7. **Carry-over instead of silent removal.** Record ids contain the row's key cells, so a
   row whose amount cell became unreadable, or a sheet that was not recognised this time,
   yields *no* counterpart for the record an earlier version produced. A partially read
   version (excluded rows or unread tables) therefore never REMOVEs such records: they are
   *carried over* - kept unchanged, still citing the version they were read from - and
   reported separately (``ApplyOutcome.records_carried_over``), so a one-cell typo cannot
   lower the open total or orphan a payment allocation. The acknowledgment is the user's
   statement that this version is complete as read; it removes the carried-over records
   (``AckReport.removed``). A complete later version (nothing excluded or unread) replaces
   the document's records as in rule 5, carried-over ones included. Documents rows are
   not economic receivables: carrying a row over keeps the *document row*; how rows become
   receivables is decided later by :mod:`jettae.evidence.links`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from jettae.app.contracts import (
    CURRENT_STATES,
    ApplyOutcome,
    ApplyState,
    ParseResult,
)
from jettae.app.dto import ChangeOutcome
from jettae.app.ports import LEDGER_ENTITIES, DocumentHead, DocumentHeadRepo, Repositories
from jettae.domain.dates import ensure_utc
from jettae.domain.errors import JettaeError, NotFoundError
from jettae.domain.hashing import content_hash
from jettae.domain.models import Change, Decision, DocumentVersion
from jettae.domain.status import ChangeKind, DocumentStatus
from jettae.evidence.snapshot import entity_of

if TYPE_CHECKING:
    from jettae.app.services import JettaeService


class DocumentStateError(JettaeError):
    """An acknowledgment that does not match the document's current state (HTTP 409)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DocumentAckRequiredError(JettaeError):
    """Approval refused: the decision cites a partially applied, unacknowledged document."""

    def __init__(self, decision_id: str, documents: list[dict[str, Any]]) -> None:
        names = ", ".join(f"{d['document_id']} v{d['version']}" for d in documents)
        super().__init__(
            f"decision {decision_id} cites documents with excluded rows or total "
            f"differences that were not acknowledged: {names}"
        )
        self.decision_id = decision_id
        self.documents = documents


@dataclass(frozen=True)
class ApplyReport:
    outcome: ApplyOutcome
    changes: tuple[Change, ...] = ()
    impact: ChangeOutcome | None = None
    head: DocumentHead | None = None


@dataclass(frozen=True)
class AckReport:
    """Result of an acknowledgment: the new head and the carried-over records it removed
    (``"<entity>:<id>"``)."""

    head: DocumentHead
    removed: tuple[str, ...] = ()
    impact: ChangeOutcome | None = None


MAX_CARRIED_LISTED = 200


def parse_fingerprint(parsed: ParseResult) -> str:
    """Identity of a parse output: records, facts, issues, totals and counts."""
    return content_hash(
        {
            "status": parsed.status.value,
            "records": list(parsed.records),
            "facts": list(parsed.facts),
            "issues": [i.model_dump(mode="json") for i in parsed.issues],
            "totals": [t.model_dump(mode="json") for t in parsed.totals],
            "counts": parsed.counts.model_dump(mode="json") if parsed.counts else None,
        }
    )


def _heads(repos: Repositories) -> DocumentHeadRepo:
    if repos.heads is None:
        raise RuntimeError("repositories have no document-head store (Repositories.heads)")
    return repos.heads


class DocumentApplier:
    def __init__(self, service: JettaeService) -> None:
        self.service = service
        self.repos = service.repos
        self.heads = _heads(service.repos)

    # ------------------------------------------------------------------ apply
    def apply(self, tenant_id: str, doc: DocumentVersion, parsed: ParseResult) -> ApplyReport:
        """Decide and apply in one tenant write transaction (see module rules)."""
        if doc.tenant_id != tenant_id:
            raise NotFoundError(doc.id)
        fp = parse_fingerprint(parsed)
        with self.repos.uow.transaction(tenant_id):
            head = self.heads.get(tenant_id, doc.document_id)
            blocked = self._not_applicable(doc, parsed, head)
            if blocked is not None:
                return ApplyReport(
                    self._outcome(blocked, doc, parsed, head, head, fp), (), None, head
                )
            if head is not None and head.doc_version_id == doc.id and head.fingerprint == fp:
                return ApplyReport(
                    self._outcome(ApplyState.UNCHANGED, doc, parsed, head, head, fp), (), None, head
                )
            changes, carried = self._changes(tenant_id, doc, parsed)
            impact = self.service.apply_change(tenant_id, changes) if changes else None
            state = self._applied_state(parsed)
            needs_ack = state is ApplyState.APPLIED_NEEDS_ACK
            new_head = DocumentHead(
                tenant_id=tenant_id,
                document_id=doc.document_id,
                doc_version_id=doc.id,
                version=doc.version,
                fingerprint=fp,
                state=state.value,
                needs_ack=needs_ack,
                applied_at=ensure_utc(self.service.clock()),
            )
            if head is not None and needs_ack and head.ack_fingerprint == fp and not carried:
                # the identical output was acknowledged before: keep that acknowledgment
                # (never while records are carried over: the acknowledgment removes them)
                new_head = replace(
                    new_head, ack_fingerprint=fp, ack_by=head.ack_by, ack_at=head.ack_at
                )
            self.heads.put(new_head)
            outcome = self._outcome(state, doc, parsed, head, new_head, fp, changes, carried)
            return ApplyReport(outcome, tuple(changes), impact, new_head)

    @staticmethod
    def _not_applicable(
        doc: DocumentVersion, parsed: ParseResult, head: DocumentHead | None
    ) -> ApplyState | None:
        if parsed.status is not DocumentStatus.PARSED:
            return ApplyState.NOT_APPLIED_PARSE
        counts = parsed.counts
        assert counts is not None  # guaranteed by ParseResult for PARSED
        if counts.tables_recognized == 0:
            return ApplyState.NOT_APPLIED_NO_TABLE
        if head is not None and doc.version < head.version:
            return ApplyState.NOT_PROMOTED_OLDER
        if counts.applied_rows == 0 and counts.excluded_rows > 0:
            return ApplyState.NOT_APPLIED_ALL_EXCLUDED
        if counts.source_rows == 0 and (parsed.totals_mismatched or counts.tables_unread):
            # Zero rows, but a 합계 row that is not zero, or another table that could not be
            # read: not a *verified* empty revision (rule 4 needs a fully read file).
            return ApplyState.NOT_APPLIED_EMPTY_UNVERIFIED
        return None

    @staticmethod
    def _applied_state(parsed: ParseResult) -> ApplyState:
        counts = parsed.counts
        assert counts is not None
        if counts.source_rows == 0:
            return ApplyState.APPLIED_EMPTY
        if parsed.issues or parsed.totals_mismatched or counts.incomplete:
            return ApplyState.APPLIED_NEEDS_ACK
        return ApplyState.APPLIED

    # ------------------------------------------------------------------ diff
    def _owned(self, tenant_id: str, doc: DocumentVersion) -> tuple[set[tuple[str, str]], set[str]]:
        """Records and facts currently in the ledger that came from this document."""
        repos = self.repos
        version_ids = {v.id for v in repos.documents.versions(tenant_id, doc.document_id)}
        version_ids.add(doc.id)
        span_facts = [
            f for vid in sorted(version_ids) for f in repos.facts.for_document(tenant_id, vid)
        ]
        owned_facts = {f.id for f in span_facts}
        owned_records: set[tuple[str, str]] = set()
        if any(f.subject_id is None for f in span_facts):
            candidates: Iterable[tuple[str, Any]] = (
                (ent, rec) for ent in LEDGER_ENTITIES for rec in repos.ledger.list(tenant_id, ent)
            )
        else:
            subjects = sorted({f.subject_id for f in span_facts if f.subject_id})
            candidates = (
                (ent, rec)
                for sid in subjects
                for ent in LEDGER_ENTITIES
                if (rec := repos.ledger.get(tenant_id, ent, sid)) is not None
            )
        for ent, rec in candidates:
            rec_facts = set(getattr(rec, "facts", ()) or ())
            if rec_facts & owned_facts:
                owned_records.add((ent, rec.id))
                owned_facts |= rec_facts  # incl. facts without a span (e.g. user options)
        return owned_records, owned_facts

    def _changes(
        self, tenant_id: str, doc: DocumentVersion, parsed: ParseResult
    ) -> tuple[list[Change], tuple[tuple[str, str], ...]]:
        """Ledger/fact changes that make ``parsed`` the document's content, and the records
        carried over (rule 7: kept although the new version has no counterpart, because the
        new version was not read completely)."""
        repos = self.repos
        owned_records, owned_facts = self._owned(tenant_id, doc)
        changes: list[Change] = []
        new_facts = set()
        for f in parsed.facts:
            if f.tenant_id != tenant_id:
                raise NotFoundError(f.id)
            new_facts.add(f.id)
            kind = ChangeKind.UPDATE if repos.facts.get(tenant_id, f.id) else ChangeKind.ADD
            changes.append(Change(kind, "fact", f.id, f))
        new_records: set[tuple[str, str]] = set()
        for r in parsed.records:
            ent = entity_of(r)
            new_records.add((ent, r.id))
            exists = repos.ledger.get(tenant_id, ent, r.id) is not None
            changes.append(Change(ChangeKind.UPDATE if exists else ChangeKind.ADD, ent, r.id, r))
        missing = sorted(owned_records - new_records)
        counts = parsed.counts
        carried: tuple[tuple[str, str], ...] = ()
        kept_facts: set[str] = set()
        if counts is not None and counts.incomplete:
            carried = tuple(missing)
            for ent, rid in carried:
                rec = repos.ledger.get(tenant_id, ent, rid)
                kept_facts |= set(getattr(rec, "facts", ()) or ())
        else:
            for ent, rid in missing:
                changes.append(Change(ChangeKind.REMOVE, ent, rid))
        for fid in sorted(owned_facts - new_facts - kept_facts):
            if repos.facts.get(tenant_id, fid) is not None:
                changes.append(Change(ChangeKind.REMOVE, "fact", fid))
        return changes, carried

    def _carried_over(
        self, tenant_id: str, doc: DocumentVersion
    ) -> tuple[list[tuple[str, str]], set[str]]:
        """Records of the document that the *current* version ``doc`` did not produce (they
        cite only earlier versions) and the facts only they use."""
        owned_records, _ = self._owned(tenant_id, doc)
        current_facts = {f.id for f in self.repos.facts.for_document(tenant_id, doc.id)}
        carried: list[tuple[str, str]] = []
        carried_facts: set[str] = set()
        current_record_facts: set[str] = set()
        for ent, rid in sorted(owned_records):
            rec = self.repos.ledger.get(tenant_id, ent, rid)
            facts = set(getattr(rec, "facts", ()) or ())
            if facts & current_facts:
                current_record_facts |= facts
            else:
                carried.append((ent, rid))
                carried_facts |= facts
        return carried, carried_facts - current_record_facts - current_facts

    # ------------------------------------------------------------------ outcome
    def _outcome(
        self,
        state: ApplyState,
        doc: DocumentVersion,
        parsed: ParseResult,
        before: DocumentHead | None,
        current: DocumentHead | None,
        fp: str,
        changes: Iterable[Change] = (),
        carried: tuple[tuple[str, str], ...] = (),
    ) -> ApplyOutcome:
        ledger = [c for c in changes if c.entity in LEDGER_ENTITIES]
        added = sum(1 for c in ledger if c.kind is ChangeKind.ADD)
        updated = sum(1 for c in ledger if c.kind is ChangeKind.UPDATE)
        removed = sum(1 for c in ledger if c.kind is ChangeKind.REMOVE)
        is_current = current is not None and current.doc_version_id == doc.id
        ack_required = bool(is_current and current is not None and current.needs_ack)
        return ApplyOutcome(
            state=state,
            message=_message(state, doc, parsed, before, current, removed, len(carried)),
            records_carried_over=len(carried),
            carried_over=tuple(f"{e}:{i}" for e, i in carried[:MAX_CARRIED_LISTED]),
            document_id=doc.document_id,
            doc_version_id=doc.id,
            version=doc.version,
            current_doc_version_id=current.doc_version_id if current else None,
            current_version=current.version if current else None,
            previous_version=before.version if before else None,
            records_added=added,
            records_updated=updated,
            records_removed=removed,
            ack_required=ack_required,
            acknowledged=bool(ack_required and current is not None and current.acknowledged),
            fingerprint=fp,
        )

    # ------------------------------------------------------------------ acknowledgment
    def acknowledge(
        self, tenant_id: str, doc_version_id: str, fingerprint: str, acknowledged_by: str
    ) -> DocumentHead:
        return self.acknowledge_report(tenant_id, doc_version_id, fingerprint, acknowledged_by).head

    def acknowledge_report(
        self, tenant_id: str, doc_version_id: str, fingerprint: str, acknowledged_by: str
    ) -> AckReport:
        """Record that a user saw the excluded rows / unread tables / total differences of
        the *current* version's exact parse output, and accept that version as complete:
        records carried over from earlier versions (rule 7) are removed now, in the same
        transaction. Optimistic: ``fingerprint`` must be the current one."""
        with self.repos.uow.transaction(tenant_id):
            doc = self.repos.documents.get(tenant_id, doc_version_id)
            if doc is None:
                raise NotFoundError(doc_version_id)
            head = self.heads.get(tenant_id, doc.document_id)
            if head is None or head.doc_version_id != doc_version_id:
                raise DocumentStateError(
                    "not_current_version",
                    "only the current version of a document can be acknowledged",
                )
            if not head.needs_ack:
                raise DocumentStateError(
                    "nothing_to_acknowledge", "this version was applied completely"
                )
            if head.fingerprint != fingerprint:
                raise DocumentStateError(
                    "stale_fingerprint", "the document was read again since it was reviewed; reload"
                )
            carried, facts = self._carried_over(tenant_id, doc)
            changes = [Change(ChangeKind.REMOVE, ent, rid) for ent, rid in carried]
            changes += [
                Change(ChangeKind.REMOVE, "fact", fid)
                for fid in sorted(facts)
                if self.repos.facts.get(tenant_id, fid) is not None
            ]
            impact = self.service.apply_change(tenant_id, changes) if changes else None
            new = replace(
                head,
                ack_fingerprint=fingerprint,
                ack_by=acknowledged_by,
                ack_at=ensure_utc(self.service.clock()),
            )
            self.heads.put(new)
            return AckReport(new, tuple(f"{e}:{i}" for e, i in carried), impact)


# ---------------------------------------------------------------------- approval policy
def approval_blockers(
    repos: Repositories, tenant_id: str, decision: Decision
) -> list[dict[str, Any]]:
    """Documents cited by ``decision`` whose current version was applied only partially
    and not acknowledged. Empty list = nothing blocks the approval.

    A fact may cite an *earlier* version of the document: that is a record carried over
    from it (rule 7), which exists only because the current version is partial. So the
    check is on the document's current head, whichever version the fact cites; the entry
    names the current version (the one to acknowledge)."""
    if repos.heads is None:
        return []
    out: list[dict[str, Any]] = []
    seen_versions: set[str] = set()
    seen_documents: set[str] = set()
    for fid in decision.facts_used:
        f = repos.facts.get(tenant_id, fid)
        if f is None or f.span is None or f.span.doc_version_id in seen_versions:
            continue
        dvid = f.span.doc_version_id
        seen_versions.add(dvid)
        doc = repos.documents.get(tenant_id, dvid)
        if doc is None or doc.document_id in seen_documents:
            continue
        seen_documents.add(doc.document_id)
        head = repos.heads.get(tenant_id, doc.document_id)
        if head is not None and head.blocks_approval:
            out.append(
                {
                    "document_id": doc.document_id,
                    "doc_version_id": head.doc_version_id,
                    "version": head.version,
                    "state": head.state,
                    "fingerprint": head.fingerprint,
                    "cited_version": doc.version,
                }
            )
    return out


def is_current_state(state: str | None) -> bool:
    return state in {s.value for s in CURRENT_STATES}


# ---------------------------------------------------------------------- messages
def _message(
    state: ApplyState,
    doc: DocumentVersion,
    parsed: ParseResult,
    before: DocumentHead | None,
    current: DocumentHead | None,
    removed: int,
    carried: int = 0,
) -> str:
    v = f"v{doc.version}"
    c = parsed.counts
    if state is ApplyState.APPLIED:
        # only reachable when every table with data was recognised and every row applied
        n = c.source_rows if c else 0
        return f"{v}을(를) 현재 문서로 반영했습니다. 원본 거래 행 {n}개를 모두 반영했습니다."
    if state is ApplyState.APPLIED_NEEDS_ACK:
        assert c is not None
        value_issues = sum(1 for i in parsed.issues if i.kind.value == "value")
        parts = []
        if c.excluded_rows:
            parts.append(f"원본 {c.source_rows}행 중 {c.excluded_rows}행을 반영하지 않았습니다")
        if c.tables_unread:
            parts.append(
                f"양식을 알아보지 못한 표 {c.tables_unread}개(데이터 행 {c.unread_rows}개)를 "
                "읽지 않았습니다"
            )
        if value_issues:
            parts.append(f"읽지 못해 비워 둔 칸이 {value_issues}개 있습니다")
        if parsed.totals_mismatched:
            parts.append(
                f"표의 합계와 행 합계가 다른 곳이 {len(parsed.totals_mismatched)}곳 있습니다"
            )
        if carried:
            parts.append(
                f"이전 버전의 기록 {carried}건은 이 버전에서 대응하는 행을 읽지 못해 지우지 않고 "
                "그대로 두었습니다(확인하면 제거됩니다)"
            )
        return (
            f"{v}을(를) 현재 문서로 반영했지만 " + ", ".join(parts) + ". 확인하기 전까지 이 문서를 "
            "근거로 한 결과는 승인할 수 없습니다."
        )
    if state is ApplyState.APPLIED_EMPTY:
        return (
            f"{v}은(는) 표 머리글은 있지만 거래 행이 0개인 정정본입니다. 이 문서의 이전 기록 "
            f"{removed}건을 제거했습니다."
        )
    if state is ApplyState.UNCHANGED:
        return f"{v}은(는) 이미 현재 문서이고 다시 읽은 결과가 같아 바뀐 내용이 없습니다."
    if state is ApplyState.NOT_PROMOTED_OLDER:
        cur = f"v{current.version}" if current else "-"
        return (
            f"{v}은(는) 현재 문서({cur})보다 오래된 버전이라 현재 장부와 확인 상태에 반영하지 "
            "않았습니다. 읽은 결과는 이 버전의 기록으로만 남깁니다."
        )
    if state is ApplyState.NOT_APPLIED_NO_TABLE:
        return (
            "인식한 거래 표가 없어 반영하지 않았습니다. 빈 정정본으로 보지 않으므로 기존 기록은 "
            "그대로입니다."
        )
    if state is ApplyState.NOT_APPLIED_EMPTY_UNVERIFIED:
        if c is not None and c.tables_unread:
            return (
                f"인식한 표의 거래 행은 0개이지만 양식을 알아보지 못한 표 {c.tables_unread}개가 "
                "있어 빈 정정본으로 확정하지 않았습니다. 기존 기록은 그대로입니다."
            )
        return (
            "거래 행이 0개인데 표의 합계는 0이 아니어서 빈 정정본으로 확정하지 않았습니다. 기존 "
            "기록은 그대로입니다."
        )
    if state is ApplyState.NOT_APPLIED_ALL_EXCLUDED:
        n = c.excluded_rows if c else 0
        return f"거래 행 {n}개를 모두 읽지 못해 반영하지 않았습니다. 기존 기록은 그대로입니다."
    return (
        f"문서를 끝까지 읽지 못해(상태 {parsed.status.value}) 반영하지 않았습니다. 기존 기록은 "
        "그대로입니다."
    )
