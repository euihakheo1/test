"""initial schema: identity (tenants, users, memberships, tokens), domain records,
decisions + history, approvals (append-only), analysis results, column mappings, jobs,
idempotency keys. Generic types only (works on SQLite and PostgreSQL).

Revision ID: 0001
Revises:
Create Date: 2026-10-06 05:22:43.456698
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "analysis_results",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("graph", sa.JSON(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_analysis_results_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_analysis_results")),
    )
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_api_tokens_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_api_tokens_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_tokens_token_hash")),
    )
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_api_tokens_tenant_id"), ["tenant_id"], unique=False)

    op.create_table(
        "approvals",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.String(length=320), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_approvals_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "id", name=op.f("pk_approvals")),
    )
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_approvals_tenant_id_decision_id"),
            ["tenant_id", "decision_id"],
            unique=False,
        )

    op.create_table(
        "column_mappings",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("doc_version_id", sa.String(length=128), nullable=False),
        sa.Column("mapping", sa.JSON(), nullable=False),
        sa.Column("confirmed_by", sa.String(length=320), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_column_mappings_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "id", name=op.f("pk_column_mappings")),
    )
    with op.batch_alter_table("column_mappings", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_column_mappings_tenant_id_doc_version_id"),
            ["tenant_id", "doc_version_id"],
            unique=False,
        )

    op.create_table(
        "decision_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=False),
        sa.Column("superseded", sa.Boolean(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_decision_history_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decision_history")),
        sa.UniqueConstraint(
            "tenant_id",
            "decision_id",
            "seq",
            name=op.f("uq_decision_history_tenant_id_decision_id_seq"),
        ),
    )
    op.create_table(
        "decisions",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("superseded", sa.Boolean(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_decisions_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "id", name=op.f("pk_decisions")),
    )
    with op.batch_alter_table("decisions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_decisions_tenant_id_status"), ["tenant_id", "status"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_decisions_tenant_id_subject_id"),
            ["tenant_id", "subject_id"],
            unique=False,
        )

    op.create_table(
        "document_versions",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=127), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("supersedes", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=128), nullable=True),
        sa.Column("doc_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("status_detail", sa.JSON(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_document_versions_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint(
            "tenant_id", "content_hash", name=op.f("uq_document_versions_tenant_id_content_hash")
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "document_id",
            "version",
            name=op.f("uq_document_versions_tenant_id_document_id_version"),
        ),
    )
    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_document_versions_tenant_id_document_id"),
            ["tenant_id", "document_id"],
            unique=False,
        )

    op.create_table(
        "facts",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=128), nullable=True),
        sa.Column("doc_version_id", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_facts_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "id", name=op.f("pk_facts")),
    )
    with op.batch_alter_table("facts", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_facts_tenant_id_doc_version_id"),
            ["tenant_id", "doc_version_id"],
            unique=False,
        )

    op.create_table(
        "idempotency_keys",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_idempotency_keys_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "endpoint", "key", name=op.f("pk_idempotency_keys")),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_version", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=320), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_jobs_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_jobs_status_run_after"), ["status", "run_after"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_jobs_tenant_id_created_at"), ["tenant_id", "created_at"], unique=False
        )

    op.create_table(
        "ledger_records",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("entity", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("counterparty", sa.String(length=255), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_ledger_records_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "entity", "id", name=op.f("pk_ledger_records")),
    )
    with op.batch_alter_table("ledger_records", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_ledger_records_tenant_id_entity_counterparty"),
            ["tenant_id", "entity", "counterparty"],
            unique=False,
        )

    op.create_table(
        "memberships",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_memberships_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_memberships_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint("tenant_id", "user_id", name=op.f("uq_memberships_tenant_id_user_id")),
    )
    with op.batch_alter_table("memberships", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_memberships_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_memberships_user_id"), ["user_id"], unique=False)

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("family_id", sa.String(length=128), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_refresh_tokens_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_refresh_tokens_family_id"), ["family_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_refresh_tokens_tenant_id"), ["tenant_id"], unique=False
        )


def downgrade() -> None:

    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_refresh_tokens_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_refresh_tokens_family_id"))

    op.drop_table("refresh_tokens")
    with op.batch_alter_table("memberships", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_memberships_user_id"))
        batch_op.drop_index(batch_op.f("ix_memberships_tenant_id"))

    op.drop_table("memberships")
    with op.batch_alter_table("ledger_records", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ledger_records_tenant_id_entity_counterparty"))

    op.drop_table("ledger_records")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_jobs_tenant_id_created_at"))
        batch_op.drop_index(batch_op.f("ix_jobs_status_run_after"))

    op.drop_table("jobs")
    op.drop_table("idempotency_keys")
    with op.batch_alter_table("facts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_facts_tenant_id_doc_version_id"))

    op.drop_table("facts")
    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_document_versions_tenant_id_document_id"))

    op.drop_table("document_versions")
    with op.batch_alter_table("decisions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_decisions_tenant_id_subject_id"))
        batch_op.drop_index(batch_op.f("ix_decisions_tenant_id_status"))

    op.drop_table("decisions")
    op.drop_table("decision_history")
    with op.batch_alter_table("column_mappings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_column_mappings_tenant_id_doc_version_id"))

    op.drop_table("column_mappings")
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_approvals_tenant_id_decision_id"))

    op.drop_table("approvals")
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_tokens_tenant_id"))

    op.drop_table("api_tokens")
    op.drop_table("analysis_results")
    op.drop_table("users")
    op.drop_table("tenants")
