"""llm_budget / llm_budget_entry: the shared LLM spending ledger (``SqlBudgetStore``).

Every worker and API process that may call a paid model reserves against one row set here
(``JETTAE_LLM_BUDGET_DB`` = the service database). A reservation is one transaction (SQLite
``BEGIN IMMEDIATE``; PostgreSQL ``SELECT ... FOR UPDATE`` on the ``llm_budget`` row), so two
processes can never both reserve the last part of the limit. Amounts are integer units of
0.0001 KRW (``reserved_e4``/``cost_e4``) so sums are exact.

The table definitions are owned by ``jettae.llm.budget_store.budget_tables()`` (the LLM
package does not depend on the service ORM). ``SqlBudgetStore`` may already have created the
tables lazily on a database that predates this revision, so ``upgrade`` skips tables that
exist (online mode only; offline SQL always renders the CREATE statements).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing() -> set[str]:
    if context.is_offline_mode():
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    have = _existing()
    if "llm_budget" not in have:
        op.create_table(
            "llm_budget",
            sa.Column("budget_id", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("budget_id"),
        )
    if "llm_budget_entry" not in have:
        op.create_table(
            "llm_budget_entry",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("budget_id", sa.String(length=64), nullable=False),
            sa.Column("model", sa.String(length=200), nullable=False),
            sa.Column("purpose", sa.String(length=200), nullable=False),
            sa.Column("reserved_e4", sa.BigInteger(), nullable=False),
            sa.Column("cost_e4", sa.BigInteger(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("owner", sa.String(length=200), server_default="", nullable=False),
            sa.Column("note", sa.Text(), server_default="", nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_llm_budget_entry_budget_id", "llm_budget_entry", ["budget_id"], unique=False
        )


def downgrade() -> None:
    op.drop_index("ix_llm_budget_entry_budget_id", table_name="llm_budget_entry")
    op.drop_table("llm_budget_entry")
    op.drop_table("llm_budget")
