"""auth_sessions: one row per browser login (refresh-token family), checked on every request.

Access tokens issued from this revision on carry the session id (``sid``). Tokens issued
before it have no session row: access tokens are refused (they expire within minutes anyway)
and their refresh tokens are refused at the next refresh, so users sign in again once after
the upgrade. Rows are not backfilled, because a pre-upgrade family carries no absolute
lifetime that could be honoured.

Revision ID: 0005_auth_sessions
Revises: 0004
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_auth_sessions"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_auth_sessions_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
    )
    with op.batch_alter_table("auth_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_auth_sessions_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_auth_sessions_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("auth_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_auth_sessions_user_id"))
        batch_op.drop_index(batch_op.f("ix_auth_sessions_tenant_id"))
    op.drop_table("auth_sessions")
