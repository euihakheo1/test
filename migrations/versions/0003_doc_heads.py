"""document_heads (current applied version per document) + document_versions.apply_state /
issue_count.

``document_heads`` makes the "which version is in the ledger" rule explicit (see
``jettae.app.doc_apply``): an older version can no longer overwrite a newer one.

Backfill: before this revision the ledger held whatever version's job finished last, which
cannot be reconstructed. The closest safe choice is the newest version whose status is
PARSED; it is marked ``state='legacy'`` with an empty fingerprint, so the next parse of that
version is applied (never treated as "unchanged").

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_heads",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("doc_version_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("needs_ack", sa.Boolean(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ack_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("ack_by", sa.String(length=320), nullable=True),
        sa.Column("ack_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_document_heads_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "document_id", name=op.f("pk_document_heads")),
    )
    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("apply_state", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("issue_count", sa.Integer(), nullable=True))

    dv = sa.table(
        "document_versions",
        sa.column("tenant_id", sa.String),
        sa.column("id", sa.String),
        sa.column("document_id", sa.String),
        sa.column("version", sa.Integer),
        sa.column("status", sa.String),
        sa.column("doc_created_at", sa.DateTime(timezone=True)),
    )
    heads = sa.table(
        "document_heads",
        sa.column("tenant_id", sa.String),
        sa.column("document_id", sa.String),
        sa.column("doc_version_id", sa.String),
        sa.column("version", sa.Integer),
        sa.column("fingerprint", sa.String),
        sa.column("state", sa.String),
        sa.column("needs_ack", sa.Boolean),
        sa.column("applied_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    v2 = dv.alias("v2")
    newest_parsed = (
        sa.select(sa.func.max(v2.c.version))
        .where(
            v2.c.tenant_id == dv.c.tenant_id,
            v2.c.document_id == dv.c.document_id,
            v2.c.status == "PARSED",
        )
        .scalar_subquery()
    )
    src = sa.select(
        dv.c.tenant_id,
        dv.c.document_id,
        dv.c.id,
        dv.c.version,
        sa.literal(""),
        sa.literal("legacy"),
        sa.false(),
        dv.c.doc_created_at,
        dv.c.doc_created_at,
        dv.c.doc_created_at,
    ).where(dv.c.status == "PARSED", dv.c.version == newest_parsed)
    op.execute(
        heads.insert().from_select(
            [
                "tenant_id",
                "document_id",
                "doc_version_id",
                "version",
                "fingerprint",
                "state",
                "needs_ack",
                "applied_at",
                "created_at",
                "updated_at",
            ],
            src,
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.drop_column("issue_count")
        batch_op.drop_column("apply_state")
    op.drop_table("document_heads")
