"""agent_investigations: Agent reviews of one decision requested from the web page.

The table mirrors ``jettae.db.orm_investigations.InvestigationRow``. Investigations are
read-only reviews; they reference a decision by id but carry no foreign key to
``decisions``: a decision may be recomputed or removed later, and the review stays as the
record of what was found at that time.

Revision ID: 0006_investigations
Revises: 0005_auth_sessions
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_investigations"
down_revision: str | None = "0005_auth_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_investigations",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("strategy", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=True),
        sa.Column("requested_by", sa.String(length=320), nullable=False),
        sa.Column("decision_result_hash", sa.String(length=64), nullable=True),
        sa.Column("findings", sa.JSON(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_agent_investigations_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "id", name=op.f("pk_agent_investigations")),
    )
    op.create_index(
        op.f("ix_agent_investigations_tenant_id_decision_id_created_at"),
        "agent_investigations",
        ["tenant_id", "decision_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_agent_investigations_tenant_id_decision_id_created_at"),
        table_name="agent_investigations",
    )
    op.drop_table("agent_investigations")
