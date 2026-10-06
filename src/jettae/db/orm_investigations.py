"""ORM table of Agent investigations (revision ``0006_investigations``).

Kept out of ``db/orm.py`` so the investigation feature can ship separately, but it uses the
same ``Base`` (one MetaData). Constraint: Alembic comparisons and ``metadata.create_all``
only see this table once this module has been imported, so every code path that compares or
creates the schema (``jettae.db.migrate.target_metadata``, the API router, the worker
handler) must import it; otherwise a migrated database looks like it has an extra table.

An investigation is a read-only review of one decision: it never writes decisions, approvals
or ledger records. The row stores what the review found (``findings``) and how much it used
(``usage``), so the web page can show it after the worker has finished.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from jettae.db.orm import ID, Base, Timestamps, UtcDateTime, tenant_fk

TABLE = "agent_investigations"


class InvestigationRow(Timestamps, Base):
    __tablename__ = TABLE
    __table_args__ = (sa.Index(None, "tenant_id", "decision_id", "created_at"),)
    tenant_id: Mapped[str] = mapped_column(ID, tenant_fk(), primary_key=True)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    decision_id: Mapped[str] = mapped_column(ID, nullable=False)
    strategy: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    mode: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    job_id: Mapped[str | None] = mapped_column(ID, nullable=True)
    requested_by: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    # result hash of the decision when the investigation was requested / when it ran: the
    # page can tell that a finished review refers to an older result
    decision_result_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    findings: Mapped[Any] = mapped_column(sa.JSON, nullable=True)
    usage: Mapped[Any] = mapped_column(sa.JSON, nullable=True)
    report: Mapped[Any] = mapped_column(sa.JSON, nullable=True)
    error: Mapped[Any] = mapped_column(sa.JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
