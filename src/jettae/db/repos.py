"""SQLAlchemy repositories implementing :mod:`jettae.app.ports`.

Every query filters on ``tenant_id``; composite primary keys start with ``tenant_id`` so a
record id can never resolve to another tenant's row. Domain objects are stored as typed
canonical JSON (:mod:`jettae.db.codec`) next to a few indexed columns.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa

from jettae.app.ports import STORED_ENTITIES, DecisionRecord, DocumentHead, Repositories
from jettae.db.codec import CODEC_VERSION, decode, decode_as, encode
from jettae.db.orm import (
    AnalysisResultRow,
    ApprovalRow,
    ColumnMappingRow,
    DecisionHistoryRow,
    DecisionRow,
    DocumentHeadRow,
    DocumentVersionRow,
    FactRow,
    LedgerRecordRow,
)
from jettae.db.session import Database
from jettae.domain.errors import TenantMismatchError
from jettae.domain.models import (
    Agreement,
    Approval,
    BankTxn,
    Decision,
    DocumentVersion,
    EvidenceLink,
    Fact,
    Invoice,
    SettlementLine,
)
from jettae.domain.money import Money, RoundingMode
from jettae.domain.status import DocKind, DocumentStatus
from jettae.evidence.deps import DependencyGraph
from jettae.evidence.engine import AnalysisResult, GroupOutcome
from jettae.evidence.snapshot import AnalysisConfig
from jettae.recon.candidates import ReconConfig
from jettae.rules.calendar_kr import default_calendar
from jettae.rules.kr_retail import builtin_registry

LEDGER_TYPES: dict[str, type[Any]] = {
    "invoice": Invoice,
    "settlement_line": SettlementLine,
    "bank_txn": BankTxn,
    "agreement": Agreement,
    "evidence_link": EvidenceLink,
}


# --------------------------------------------------------------------------- documents
def _doc_from_row(r: DocumentVersionRow) -> DocumentVersion:
    return DocumentVersion(
        id=r.id,
        tenant_id=r.tenant_id,
        document_id=r.document_id,
        version=r.version,
        content_hash=r.content_hash,
        filename=r.filename,
        media_type=r.media_type,
        kind=DocKind(r.kind),
        created_at=r.doc_created_at,
        size=r.size,
        supersedes=r.supersedes,
        status=DocumentStatus(r.status),
        storage_key=r.storage_key,
    )


def db_safe_text(text: str | None) -> str | None:
    """PostgreSQL text cannot hold NUL; SQLite can. Replace NUL 1:1 with U+FFFD on both
    databases so stored document text (and character offsets into it) is identical."""
    if text is None or "\x00" not in text:
        return text
    return text.replace("\x00", "\ufffd")


class SqlDocuments:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, doc: DocumentVersion, text: str | None = None) -> None:
        with self.db.session() as s:
            s.add(
                DocumentVersionRow(
                    tenant_id=doc.tenant_id,
                    id=doc.id,
                    document_id=doc.document_id,
                    version=doc.version,
                    content_hash=doc.content_hash,
                    filename=doc.filename,
                    media_type=doc.media_type,
                    kind=doc.kind.value,
                    size=doc.size,
                    supersedes=doc.supersedes,
                    status=doc.status.value,
                    storage_key=doc.storage_key,
                    doc_created_at=doc.created_at,
                    text=db_safe_text(text),
                    schema_version=CODEC_VERSION,
                )
            )
            s.flush()

    def _row(self, s: Any, tenant_id: str, doc_version_id: str) -> DocumentVersionRow | None:
        return s.get(DocumentVersionRow, (tenant_id, doc_version_id))

    def get(self, tenant_id: str, doc_version_id: str) -> DocumentVersion | None:
        with self.db.session() as s:
            r = self._row(s, tenant_id, doc_version_id)
            return _doc_from_row(r) if r else None

    def latest(self, tenant_id: str, document_id: str) -> DocumentVersion | None:
        with self.db.session() as s:
            r = s.scalars(
                sa.select(DocumentVersionRow)
                .where(
                    DocumentVersionRow.tenant_id == tenant_id,
                    DocumentVersionRow.document_id == document_id,
                )
                .order_by(DocumentVersionRow.version.desc())
                .limit(1)
            ).first()
            return _doc_from_row(r) if r else None

    def list(self, tenant_id: str) -> list[DocumentVersion]:
        with self.db.session() as s:
            rows = s.scalars(
                sa.select(DocumentVersionRow)
                .where(DocumentVersionRow.tenant_id == tenant_id)
                .order_by(DocumentVersionRow.id)
            )
            return [_doc_from_row(r) for r in rows]

    def get_text(self, tenant_id: str, doc_version_id: str) -> str | None:
        with self.db.session() as s:
            r = self._row(s, tenant_id, doc_version_id)
            return r.text if r else None

    # -- adapter extras (used by the API / worker, not part of the port) ---------------------
    def find_by_hash(self, tenant_id: str, content_hash: str) -> DocumentVersion | None:
        with self.db.session() as s:
            # several versions may share content (A -> B -> A); prefer the newest
            r = s.scalars(
                sa.select(DocumentVersionRow)
                .where(
                    DocumentVersionRow.tenant_id == tenant_id,
                    DocumentVersionRow.content_hash == content_hash,
                )
                .order_by(
                    DocumentVersionRow.doc_created_at.desc(), DocumentVersionRow.version.desc()
                )
            ).first()
            return _doc_from_row(r) if r else None

    def versions(self, tenant_id: str, document_id: str) -> builtins.list[DocumentVersion]:
        with self.db.session() as s:
            rows = s.scalars(
                sa.select(DocumentVersionRow)
                .where(
                    DocumentVersionRow.tenant_id == tenant_id,
                    DocumentVersionRow.document_id == document_id,
                )
                .order_by(DocumentVersionRow.version)
            )
            return [_doc_from_row(r) for r in rows]

    def page_latest(
        self, tenant_id: str, *, after: str | None, limit: int
    ) -> builtins.list[DocumentVersion]:
        """Latest version of each document, ordered by document_id (cursor = document_id)."""
        with self.db.session() as s:
            sub = (
                sa.select(
                    DocumentVersionRow.document_id,
                    sa.func.max(DocumentVersionRow.version).label("v"),
                )
                .where(DocumentVersionRow.tenant_id == tenant_id)
                .group_by(DocumentVersionRow.document_id)
                .subquery()
            )
            q = (
                sa.select(DocumentVersionRow)
                .join(
                    sub,
                    sa.and_(
                        DocumentVersionRow.document_id == sub.c.document_id,
                        DocumentVersionRow.version == sub.c.v,
                    ),
                )
                .where(DocumentVersionRow.tenant_id == tenant_id)
                .order_by(DocumentVersionRow.document_id)
                .limit(limit)
            )
            if after is not None:
                q = q.where(DocumentVersionRow.document_id > after)
            return [_doc_from_row(r) for r in s.scalars(q)]

    def set_status(
        self,
        tenant_id: str,
        doc_version_id: str,
        status: DocumentStatus,
        detail: Mapping[str, Any] | None = None,
        text: str | None = None,
        *,
        apply_state: str | None = None,
        issue_count: int | None = None,
    ) -> None:
        with self.db.session() as s:
            r = self._row(s, tenant_id, doc_version_id)
            if r is None:
                raise LookupError(doc_version_id)
            r.status = status.value
            r.status_detail = dict(detail) if detail is not None else None
            r.apply_state = apply_state
            r.issue_count = issue_count
            if text is not None:
                r.text = db_safe_text(text)

    def apply_states(
        self, tenant_id: str, doc_version_ids: Sequence[str]
    ) -> dict[str, tuple[str | None, int | None]]:
        """``{doc_version_id: (apply_state, issue_count)}`` for listing pages."""
        if not doc_version_ids:
            return {}
        with self.db.session() as s:
            rows = s.execute(
                sa.select(
                    DocumentVersionRow.id,
                    DocumentVersionRow.apply_state,
                    DocumentVersionRow.issue_count,
                ).where(
                    DocumentVersionRow.tenant_id == tenant_id,
                    DocumentVersionRow.id.in_(list(doc_version_ids)),
                )
            )
            return {r[0]: (r[1], r[2]) for r in rows}

    def status_detail(self, tenant_id: str, doc_version_id: str) -> Any:
        with self.db.session() as s:
            r = self._row(s, tenant_id, doc_version_id)
            return r.status_detail if r else None


# --------------------------------------------------------------------------- facts
class SqlFacts:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add_many(self, facts: Sequence[Fact]) -> None:
        for f in facts:
            self.upsert(f)

    def upsert(self, fact: Fact) -> None:
        with self.db.session() as s:
            row = s.get(FactRow, (fact.tenant_id, fact.id))
            payload = encode(fact)
            doc = fact.span.doc_version_id if fact.span else None
            if row is None:
                s.add(
                    FactRow(
                        tenant_id=fact.tenant_id,
                        id=fact.id,
                        kind=fact.kind,
                        subject_id=fact.subject_id,
                        doc_version_id=doc,
                        payload=payload,
                        schema_version=CODEC_VERSION,
                    )
                )
            else:
                row.kind, row.subject_id, row.doc_version_id = fact.kind, fact.subject_id, doc
                row.payload, row.schema_version = payload, CODEC_VERSION
            s.flush()

    def remove(self, tenant_id: str, fact_id: str) -> None:
        with self.db.session() as s:
            s.execute(
                sa.delete(FactRow).where(FactRow.tenant_id == tenant_id, FactRow.id == fact_id)
            )

    def get(self, tenant_id: str, fact_id: str) -> Fact | None:
        with self.db.session() as s:
            r = s.get(FactRow, (tenant_id, fact_id))
            return decode_as(Fact, r.payload) if r else None

    def list(self, tenant_id: str) -> list[Fact]:
        with self.db.session() as s:
            rows = s.scalars(
                sa.select(FactRow).where(FactRow.tenant_id == tenant_id).order_by(FactRow.id)
            )
            return [decode_as(Fact, r.payload) for r in rows]

    def for_document(self, tenant_id: str, doc_version_id: str) -> builtins.list[Fact]:
        with self.db.session() as s:
            rows = s.scalars(
                sa.select(FactRow)
                .where(FactRow.tenant_id == tenant_id, FactRow.doc_version_id == doc_version_id)
                .order_by(FactRow.id)
            )
            return [decode_as(Fact, r.payload) for r in rows]


# --------------------------------------------------------------------------- ledger
class SqlLedger:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _check(entity: str) -> type[Any]:
        if entity not in STORED_ENTITIES:
            raise ValueError(f"unknown ledger entity {entity}")
        return LEDGER_TYPES[entity]

    def upsert(self, entity: str, record: Any) -> None:
        typ = self._check(entity)
        if not isinstance(record, typ):
            raise TypeError(f"{entity} expects {typ.__name__}")
        with self.db.session() as s:
            row = s.get(LedgerRecordRow, (record.tenant_id, entity, record.id))
            payload = encode(record)
            cp = getattr(record, "counterparty", None)
            if row is None:
                s.add(
                    LedgerRecordRow(
                        tenant_id=record.tenant_id,
                        entity=entity,
                        id=record.id,
                        counterparty=cp,
                        payload=payload,
                        schema_version=CODEC_VERSION,
                    )
                )
            else:
                row.counterparty, row.payload, row.schema_version = cp, payload, CODEC_VERSION
            s.flush()

    def remove(self, tenant_id: str, entity: str, record_id: str) -> None:
        self._check(entity)
        with self.db.session() as s:
            s.execute(
                sa.delete(LedgerRecordRow).where(
                    LedgerRecordRow.tenant_id == tenant_id,
                    LedgerRecordRow.entity == entity,
                    LedgerRecordRow.id == record_id,
                )
            )

    def get(self, tenant_id: str, entity: str, record_id: str) -> Any | None:
        typ = self._check(entity)
        with self.db.session() as s:
            r = s.get(LedgerRecordRow, (tenant_id, entity, record_id))
            return decode(typ, r.payload) if r else None

    def list(self, tenant_id: str, entity: str) -> list[Any]:
        typ = self._check(entity)
        with self.db.session() as s:
            rows = s.scalars(
                sa.select(LedgerRecordRow)
                .where(LedgerRecordRow.tenant_id == tenant_id, LedgerRecordRow.entity == entity)
                .order_by(LedgerRecordRow.id)
            )
            return [decode(typ, r.payload) for r in rows]


# --------------------------------------------------------------------------- decisions
class SqlDecisions:
    """Same semantics as ``app.memory.MemDecisions``: current set + append-only history."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save_current(
        self, tenant_id: str, decisions: Mapping[str, Decision], recorded_at: datetime
    ) -> None:
        for d in decisions.values():
            if d.tenant_id != tenant_id:
                raise TenantMismatchError(d.id)
        with self.db.session() as s:
            rows = {
                r.id: r
                for r in s.scalars(sa.select(DecisionRow).where(DecisionRow.tenant_id == tenant_id))
            }
            last = self._last_history(s, tenant_id)
            for did, dec in decisions.items():
                payload = encode(dec)
                row = rows.get(did)
                if row is None:
                    s.add(
                        DecisionRow(
                            tenant_id=tenant_id,
                            id=did,
                            subject_id=dec.subject_id,
                            status=dec.status.value,
                            result_hash=dec.result_hash,
                            snapshot_hash=dec.snapshot_hash,
                            superseded=False,
                            recorded_at=recorded_at,
                            payload=payload,
                            schema_version=CODEC_VERSION,
                        )
                    )
                elif (
                    row.superseded
                    or row.result_hash != dec.result_hash
                    or row.snapshot_hash != dec.snapshot_hash
                    or row.schema_version != CODEC_VERSION
                ):
                    row.subject_id, row.status = dec.subject_id, dec.status.value
                    row.result_hash, row.snapshot_hash = dec.result_hash, dec.snapshot_hash
                    row.superseded, row.recorded_at = False, recorded_at
                    row.payload, row.schema_version = payload, CODEC_VERSION
                h = last.get(did)
                if h is None or h[1] != dec.result_hash or h[2]:
                    self._append(s, tenant_id, did, (h[0] + 1) if h else 1, dec, recorded_at, False)
            for did, row in rows.items():
                if did in decisions or row.superseded:
                    continue
                row.superseded = True
                old = decode_as(Decision, row.payload)
                h = last.get(did)
                self._append(s, tenant_id, did, (h[0] + 1) if h else 1, old, recorded_at, True)
            s.flush()

    @staticmethod
    def _last_history(s: Any, tenant_id: str) -> dict[str, tuple[int, str, bool]]:
        sub = (
            sa.select(
                DecisionHistoryRow.decision_id, sa.func.max(DecisionHistoryRow.seq).label("m")
            )
            .where(DecisionHistoryRow.tenant_id == tenant_id)
            .group_by(DecisionHistoryRow.decision_id)
            .subquery()
        )
        q = (
            sa.select(
                DecisionHistoryRow.decision_id,
                DecisionHistoryRow.seq,
                DecisionHistoryRow.result_hash,
                DecisionHistoryRow.superseded,
            )
            .join(
                sub,
                sa.and_(
                    DecisionHistoryRow.decision_id == sub.c.decision_id,
                    DecisionHistoryRow.seq == sub.c.m,
                ),
            )
            .where(DecisionHistoryRow.tenant_id == tenant_id)
        )
        return {r[0]: (r[1], r[2], r[3]) for r in s.execute(q)}

    @staticmethod
    def _append(
        s: Any,
        tenant_id: str,
        did: str,
        seq: int,
        dec: Decision,
        recorded_at: datetime,
        superseded: bool,
    ) -> None:
        s.add(
            DecisionHistoryRow(
                tenant_id=tenant_id,
                decision_id=did,
                seq=seq,
                result_hash=dec.result_hash,
                superseded=superseded,
                recorded_at=recorded_at,
                payload=encode(dec),
                schema_version=CODEC_VERSION,
            )
        )

    def current(self, tenant_id: str) -> dict[str, Decision]:
        with self.db.session() as s:
            rows = s.scalars(
                sa.select(DecisionRow)
                .where(DecisionRow.tenant_id == tenant_id, DecisionRow.superseded.is_(False))
                .order_by(DecisionRow.id)
            )
            return {r.id: decode_as(Decision, r.payload) for r in rows}

    def get_current(self, tenant_id: str, decision_id: str) -> Decision | None:
        with self.db.session() as s:
            r = s.get(DecisionRow, (tenant_id, decision_id))
            if r is None or r.superseded:
                return None
            return decode_as(Decision, r.payload)

    def history(self, tenant_id: str, decision_id: str) -> list[DecisionRecord]:
        with self.db.session() as s:
            rows = s.scalars(
                sa.select(DecisionHistoryRow)
                .where(
                    DecisionHistoryRow.tenant_id == tenant_id,
                    DecisionHistoryRow.decision_id == decision_id,
                )
                .order_by(DecisionHistoryRow.seq)
            )
            return [
                DecisionRecord(decode_as(Decision, r.payload), r.recorded_at, r.superseded)
                for r in rows
            ]

    def is_superseded(self, tenant_id: str, decision_id: str) -> bool:
        with self.db.session() as s:
            r = s.get(DecisionRow, (tenant_id, decision_id))
            return bool(r is not None and r.superseded)

    # -- adapter extras -----------------------------------------------------------------------
    def page_ids(
        self,
        tenant_id: str,
        *,
        after: str | None,
        limit: int,
        statuses: Sequence[str] = (),
        subject_id: str | None = None,
        include_superseded: bool = False,
    ) -> list[str]:
        with self.db.session() as s:
            q = sa.select(DecisionRow.id).where(DecisionRow.tenant_id == tenant_id)
            if not include_superseded:
                q = q.where(DecisionRow.superseded.is_(False))
            if statuses:
                q = q.where(DecisionRow.status.in_(list(statuses)))
            if subject_id is not None:
                q = q.where(DecisionRow.subject_id == subject_id)
            if after is not None:
                q = q.where(DecisionRow.id > after)
            return list(s.scalars(q.order_by(DecisionRow.id).limit(limit)))


# --------------------------------------------------------------------------- approvals
class SqlApprovals:
    """Append-only approval log."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, approval: Approval) -> None:
        if not approval.id:
            raise ValueError("approval id required")
        with self.db.session() as s:
            seq = s.scalar(
                sa.select(sa.func.coalesce(sa.func.max(ApprovalRow.seq), 0)).where(
                    ApprovalRow.tenant_id == approval.tenant_id
                )
            )
            s.add(
                ApprovalRow(
                    tenant_id=approval.tenant_id,
                    id=approval.id,
                    seq=int(seq or 0) + 1,
                    decision_id=approval.decision_id,
                    result_hash=approval.result_hash,
                    snapshot_hash=approval.snapshot_hash,
                    approved_by=approval.approved_by,
                    approved_at=approval.approved_at,
                )
            )
            s.flush()

    def list_for(self, tenant_id: str, decision_id: str) -> list[Approval]:
        with self.db.session() as s:
            rows = s.scalars(
                sa.select(ApprovalRow)
                .where(ApprovalRow.tenant_id == tenant_id, ApprovalRow.decision_id == decision_id)
                .order_by(ApprovalRow.seq)
            )
            return [
                Approval(
                    decision_id=r.decision_id,
                    result_hash=r.result_hash,
                    snapshot_hash=r.snapshot_hash,
                    approved_by=r.approved_by,
                    approved_at=r.approved_at,
                    tenant_id=r.tenant_id,
                    id=r.id,
                )
                for r in rows
            ]


# --------------------------------------------------------------------------- results
def encode_config(cfg: AnalysisConfig) -> dict[str, Any]:
    rc = cfg.recon
    return {
        "as_of": encode(cfg.as_of),
        "rollover": cfg.rollover,
        "rounding": cfg.rounding.value,
        "known_at": encode(cfg.known_at),
        "recon": {
            "days_before": rc.days_before,
            "days_after": rc.days_after,
            "fee_tolerance": encode(rc.fee_tolerance),
            "max_candidates": rc.max_candidates,
            "max_subset_size": rc.max_subset_size,
            "max_nodes": rc.max_nodes,
            "time_limit_s": repr(rc.time_limit_s),  # float kept as text, never hashed
        },
        "registry_fp": cfg.registry.fingerprint(),
        "calendar_fp": cfg.calendar.fingerprint(),
    }


def decode_config(data: Mapping[str, Any]) -> AnalysisConfig | None:
    """Rebuild a config with the built-in registry/calendar; ``None`` if those changed since
    the result was stored (then the caller must recompute in full)."""
    registry, calendar = builtin_registry(), default_calendar()
    if data.get("registry_fp") != registry.fingerprint():
        return None
    if data.get("calendar_fp") != calendar.fingerprint():
        return None
    r = data["recon"]
    recon = ReconConfig(
        days_before=r["days_before"],
        days_after=r["days_after"],
        fee_tolerance=decode(Money, r["fee_tolerance"]),
        max_candidates=r["max_candidates"],
        max_subset_size=r["max_subset_size"],
        max_nodes=r["max_nodes"],
        time_limit_s=float(r["time_limit_s"]),
    )
    return AnalysisConfig(
        as_of=decode(date | None, data["as_of"]),
        rollover=data["rollover"],
        rounding=RoundingMode(data["rounding"]),
        known_at=decode(date | None, data["known_at"]),
        recon=recon,
        registry=registry,
        calendar=calendar,
    )


class SqlResults:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, tenant_id: str, result: AnalysisResult, config: AnalysisConfig) -> None:
        if result.tenant_id != tenant_id:
            raise TenantMismatchError(tenant_id)
        body = {
            "tenant_id": result.tenant_id,
            "snapshot_hash": result.snapshot_hash,
            "decisions": {k: encode(v) for k, v in result.decisions.items()},
            "groups": {k: encode(v) for k, v in result.groups.items()},
            "unattributed": encode(result.unattributed),
        }
        with self.db.session() as s:
            row = s.get(AnalysisResultRow, tenant_id)
            if row is None:
                row = AnalysisResultRow(tenant_id=tenant_id)
                s.add(row)
            row.snapshot_hash = result.snapshot_hash
            row.result = body
            row.graph = result.graph.to_dict()
            row.config = encode_config(config)
            row.schema_version = CODEC_VERSION
            s.flush()

    def load(self, tenant_id: str) -> tuple[AnalysisResult, AnalysisConfig] | None:
        with self.db.session() as s:
            row = s.get(AnalysisResultRow, tenant_id)
            if row is None or row.schema_version != CODEC_VERSION:
                return None
            cfg = decode_config(row.config)
            if cfg is None:
                return None
            body = row.result
            result = AnalysisResult(
                tenant_id=body["tenant_id"],
                snapshot_hash=body["snapshot_hash"],
                decisions={k: decode_as(Decision, v) for k, v in body["decisions"].items()},
                groups={k: decode_as(GroupOutcome, v) for k, v in body["groups"].items()},
                unattributed=decode(tuple[tuple[str, str], ...], body["unattributed"]),
                graph=DependencyGraph.from_dict(row.graph),
            )
            return result, cfg


# --------------------------------------------------------------------------- document heads
def _head_from_row(r: DocumentHeadRow) -> DocumentHead:
    return DocumentHead(
        tenant_id=r.tenant_id,
        document_id=r.document_id,
        doc_version_id=r.doc_version_id,
        version=r.version,
        fingerprint=r.fingerprint,
        state=r.state,
        needs_ack=r.needs_ack,
        applied_at=r.applied_at,
        ack_fingerprint=r.ack_fingerprint,
        ack_by=r.ack_by,
        ack_at=r.ack_at,
    )


class SqlDocumentHeads:
    """Current-version pointers. ``put`` is called by ``jettae.app.doc_apply`` inside the
    tenant write transaction, after it re-read the head in that same transaction."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, tenant_id: str, document_id: str) -> DocumentHead | None:
        with self.db.session() as s:
            r = s.get(DocumentHeadRow, (tenant_id, document_id), populate_existing=True)
            return _head_from_row(r) if r else None

    def get_many(self, tenant_id: str, document_ids: Sequence[str]) -> dict[str, DocumentHead]:
        if not document_ids:
            return {}
        with self.db.session() as s:
            rows = s.scalars(
                sa.select(DocumentHeadRow).where(
                    DocumentHeadRow.tenant_id == tenant_id,
                    DocumentHeadRow.document_id.in_(list(document_ids)),
                )
            )
            return {r.document_id: _head_from_row(r) for r in rows}

    def put(self, head: DocumentHead) -> None:
        with self.db.session() as s:
            r = s.get(DocumentHeadRow, (head.tenant_id, head.document_id))
            if r is None:
                r = DocumentHeadRow(tenant_id=head.tenant_id, document_id=head.document_id)
                s.add(r)
            r.doc_version_id, r.version = head.doc_version_id, head.version
            r.fingerprint, r.state, r.needs_ack = head.fingerprint, head.state, head.needs_ack
            r.applied_at = head.applied_at
            r.ack_fingerprint, r.ack_by, r.ack_at = head.ack_fingerprint, head.ack_by, head.ack_at
            s.flush()


# --------------------------------------------------------------------------- unit of work
class SqlUnitOfWork:
    def __init__(self, db: Database) -> None:
        self.db = db

    @contextmanager
    def transaction(self, tenant_id: str) -> Iterator[None]:
        with self.db.write(tenant_id):
            yield


def sql_repositories(db: Database, blobs: Any) -> Repositories:
    return Repositories(
        documents=SqlDocuments(db),
        blobs=blobs,
        facts=SqlFacts(db),
        ledger=SqlLedger(db),
        decisions=SqlDecisions(db),
        approvals=SqlApprovals(db),
        results=SqlResults(db),
        uow=SqlUnitOfWork(db),
        heads=SqlDocumentHeads(db),
    )


# --------------------------------------------------------------------------- column mappings
class SqlMappings:
    """User-confirmed column mappings per document version (latest wins, history kept)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def confirm(
        self,
        tenant_id: str,
        doc_version_id: str,
        mapping: Mapping[str, Any],
        confirmed_by: str,
        mapping_id: str,
    ) -> None:
        with self.db.session() as s:
            s.add(
                ColumnMappingRow(
                    tenant_id=tenant_id,
                    id=mapping_id,
                    doc_version_id=doc_version_id,
                    mapping=dict(mapping),
                    confirmed_by=confirmed_by,
                )
            )
            s.flush()

    def latest(self, tenant_id: str, doc_version_id: str) -> dict[str, Any] | None:
        with self.db.session() as s:
            r = s.scalars(
                sa.select(ColumnMappingRow)
                .where(
                    ColumnMappingRow.tenant_id == tenant_id,
                    ColumnMappingRow.doc_version_id == doc_version_id,
                )
                .order_by(ColumnMappingRow.created_at.desc(), ColumnMappingRow.id.desc())
                .limit(1)
            ).first()
            if r is None:
                return None
            return {
                "id": r.id,
                "mapping": r.mapping,
                "confirmed_by": r.confirmed_by,
                "confirmed_at": r.created_at,
            }
