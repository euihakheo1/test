"""SQLAlchemy 2 ORM schema. Portable between SQLite and PostgreSQL: only generic types
(String, Text, Integer, BigInteger, Boolean, JSON, timezone-aware DateTime via
:class:`UtcDateTime`). Every domain table carries ``tenant_id`` (first column of the primary
key or a foreign key to ``tenants``). ``users`` is the only global table: a person may be a
member of several tenants through ``memberships`` (which carries ``tenant_id``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetime on every backend (SQLite stores naive UTC text)."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be stored; use UTC-aware datetimes")
        v = value.astimezone(UTC)
        return v.replace(tzinfo=None) if dialect.name == "sqlite" else v

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING)


ID = sa.String(128)


def tenant_fk() -> sa.ForeignKey:
    return sa.ForeignKey("tenants.id", ondelete="CASCADE")


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=_now, onupdate=_now, nullable=False
    )


# ------------------------------------------------------------------------------- identity
class TenantRow(Timestamps, Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)


class UserRow(Timestamps, Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)


class MembershipRow(Timestamps, Base):
    __tablename__ = "memberships"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "user_id"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(
        ID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(sa.String(20), nullable=False)


class RefreshTokenRow(Timestamps, Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(
        ID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[str] = mapped_column(ID, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    replaced_by: Mapped[str | None] = mapped_column(ID, nullable=True)


class ApiTokenRow(Timestamps, Base):
    __tablename__ = "api_tokens"
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(
        ID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


# ------------------------------------------------------------------------------- domain
class DocumentVersionRow(Timestamps, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "document_id", "version"),
        # content lookup for upload dedupe. Not unique: a correction may restore the content
        # of an earlier version (A -> B -> A) and must become a new version (migration 0002).
        sa.Index(None, "tenant_id", "content_hash"),
        sa.Index(None, "tenant_id", "document_id"),
    )
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), primary_key=True)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    document_id: Mapped[str] = mapped_column(ID, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    filename: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    size: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    supersedes: Mapped[str | None] = mapped_column(ID, nullable=True)
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    doc_created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status_detail: Mapped[Any] = mapped_column(sa.JSON, nullable=True)
    schema_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    # What applying this version did (jettae.app.contracts.ApplyState); ``status`` only says
    # whether the format was read. Row issues/totals/counts live in ``status_detail``.
    apply_state: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    issue_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)


class DocumentHeadRow(Timestamps, Base):
    """Current-version pointer: the one version per document whose rows are in the ledger
    (see ``jettae.app.doc_apply``). Written only inside the tenant write transaction."""

    __tablename__ = "document_heads"
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), primary_key=True)
    document_id: Mapped[str] = mapped_column(ID, primary_key=True)
    doc_version_id: Mapped[str] = mapped_column(ID, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    needs_ack: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    applied_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    ack_fingerprint: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    ack_by: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    ack_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class FactRow(Timestamps, Base):
    __tablename__ = "facts"
    __table_args__ = (sa.Index(None, "tenant_id", "doc_version_id"),)
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), primary_key=True)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    kind: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    subject_id: Mapped[str | None] = mapped_column(ID, nullable=True)
    doc_version_id: Mapped[str | None] = mapped_column(ID, nullable=True)
    payload: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    schema_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class LedgerRecordRow(Timestamps, Base):
    """Invoice / SettlementLine / BankTxn / Agreement records, plus user EvidenceLink
    confirmations (entity ``evidence_link``, counterparty NULL). The record id is the
    natural/external id assigned by ingestion (e.g. bank transaction id), so the primary key
    (tenant_id, entity, id) makes re-imports idempotent."""

    __tablename__ = "ledger_records"
    __table_args__ = (sa.Index(None, "tenant_id", "entity", "counterparty"),)
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), primary_key=True)
    entity: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    counterparty: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    payload: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    schema_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class DecisionRow(Timestamps, Base):
    __tablename__ = "decisions"
    __table_args__ = (
        sa.Index(None, "tenant_id", "status"),
        sa.Index(None, "tenant_id", "subject_id"),
    )
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), primary_key=True)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    subject_id: Mapped[str] = mapped_column(ID, nullable=False)
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    result_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    superseded: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    recorded_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    payload: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    schema_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class DecisionHistoryRow(Timestamps, Base):
    __tablename__ = "decision_history"
    __table_args__ = (sa.UniqueConstraint("tenant_id", "decision_id", "seq"),)
    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), nullable=False)
    decision_id: Mapped[str] = mapped_column(ID, nullable=False)
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    result_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    superseded: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    recorded_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    payload: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    schema_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class ApprovalRow(Timestamps, Base):
    """Append-only. Rows are never updated; validity is computed from result hashes."""

    __tablename__ = "approvals"
    __table_args__ = (sa.Index(None, "tenant_id", "decision_id"),)
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), primary_key=True)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    seq: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    decision_id: Mapped[str] = mapped_column(ID, nullable=False)
    result_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    approved_by: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class AnalysisResultRow(Timestamps, Base):
    __tablename__ = "analysis_results"
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), primary_key=True)
    snapshot_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    result: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    graph: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    config: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    schema_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class ColumnMappingRow(Timestamps, Base):
    __tablename__ = "column_mappings"
    __table_args__ = (sa.Index(None, "tenant_id", "doc_version_id"),)
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), primary_key=True)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    doc_version_id: Mapped[str] = mapped_column(ID, nullable=False)
    mapping: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    confirmed_by: Mapped[str] = mapped_column(sa.String(320), nullable=False)


# ------------------------------------------------------------------------------- jobs
class JobRow(Timestamps, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        sa.Index(None, "status", "run_after"),
        sa.Index(None, "tenant_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), nullable=False)
    type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    payload: Mapped[Any] = mapped_column(sa.JSON, nullable=False)
    result: Mapped[Any] = mapped_column(sa.JSON, nullable=True)
    error: Mapped[Any] = mapped_column(sa.JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    run_after: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    lease_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    created_by: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class IdempotencyKeyRow(Timestamps, Base):
    __tablename__ = "idempotency_keys"
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), primary_key=True)
    endpoint: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    key: Mapped[str] = mapped_column(sa.String(200), primary_key=True)
    request_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    response_status: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    response_body: Mapped[Any] = mapped_column(sa.JSON, nullable=True)


metadata = Base.metadata
