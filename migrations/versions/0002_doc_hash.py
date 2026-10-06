"""document_versions: drop UNIQUE(tenant_id, content_hash), add a plain index.

A correction upload that restores earlier content (v1=A, v2=B, v3=A) must create a new
version; dedupe is decided in code (latest version of the same document) inside the
tenant-serialised write transaction.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("uq_document_versions_tenant_id_content_hash"), type_="unique"
        )
        batch_op.create_index(
            batch_op.f("ix_document_versions_tenant_id_content_hash"),
            ["tenant_id", "content_hash"],
            unique=False,
        )


def downgrade() -> None:
    # Fails if a tenant now stores the same content twice (A -> B -> A corrections).
    with op.batch_alter_table("document_versions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_document_versions_tenant_id_content_hash"))
        batch_op.create_unique_constraint(
            batch_op.f("uq_document_versions_tenant_id_content_hash"),
            ["tenant_id", "content_hash"],
        )
