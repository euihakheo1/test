"""Alembic upgrade/downgrade on SQLite; schema equals the ORM metadata."""

from __future__ import annotations

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from jettae.db import migrate
from jettae.db.orm import metadata
from jettae.db.session import make_engine


def _tables(engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_upgrade_downgrade_roundtrip(tmp_path):
    url = f"sqlite:///{tmp_path.as_posix()}/m.db"
    migrate.upgrade(url)
    engine = make_engine(url)
    try:
        assert migrate.current_revision(engine) == migrate.head_revision()
        expected = set(metadata.tables)
        assert expected <= _tables(engine)
        with engine.connect() as conn:
            diff = compare_metadata(
                MigrationContext.configure(conn, opts={"compare_type": True}),
                migrate.target_metadata(),
            )
        assert diff == [], diff
        # every table except users/tenants carries tenant_id
        for name, table in metadata.tables.items():
            if name not in ("users", "tenants"):
                assert "tenant_id" in table.c, name
        migrate.downgrade(url, "base")
        assert _tables(engine) - {"alembic_version"} == set()
        migrate.upgrade(url)
        assert expected <= _tables(engine)
    finally:
        engine.dispose()


def test_offline_sql_has_no_dialect_specific_types(tmp_path):
    """Render the migration as PostgreSQL DDL (offline mode, no server needed)."""
    import io

    from alembic import command

    buf = io.StringIO()
    cfg = migrate.alembic_config("postgresql+psycopg://u:p@localhost/db")
    cfg.output_buffer = buf
    command.upgrade(cfg, "head", sql=True)
    ddl = buf.getvalue()
    assert "CREATE TABLE document_versions" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl and "JSON" in ddl
    assert "JSONB" not in ddl and "AUTOINCREMENT" not in ddl


def test_0003_backfills_current_version_and_downgrades(tmp_path):
    """Upgrade 0002 -> 0003 on SQLite: the newest PARSED version of each document becomes
    its current version (state 'legacy', empty fingerprint); downgrade drops the table."""

    url = f"sqlite:///{tmp_path.as_posix()}/m3.db"
    migrate.upgrade(url, "0002")
    engine = make_engine(url)
    now = "2025-11-01 00:00:00.000000"  # naive UTC text, as UtcDateTime stores it
    try:
        with engine.begin() as c:
            c.execute(
                sa.text(
                    "INSERT INTO tenants (id, name, created_at, updated_at) VALUES (:i, :n, :t, :t)"
                ),
                {"i": "t1", "n": "T", "t": now},
            )
            for vid, ver, status in (
                ("v1", 1, "PARSED"),
                ("v2", 2, "PARSED"),
                ("v3", 3, "CORRUPT"),
            ):
                c.execute(
                    sa.text(
                        "INSERT INTO document_versions (tenant_id, id, document_id, version, "
                        "content_hash, filename, media_type, kind, size, status, doc_created_at, "
                        "schema_version, created_at, updated_at) VALUES ('t1', :id, 'd1', :v, "
                        ":h, 'f.csv', 'text/csv', 'settlement', 1, :s, :t, 1, :t, :t)"
                    ),
                    {"id": vid, "v": ver, "h": vid * 32, "s": status, "t": now},
                )
        migrate.upgrade(url)
        with engine.connect() as c:
            rows = c.execute(
                sa.text(
                    "SELECT document_id, doc_version_id, version, state, fingerprint "
                    "FROM document_heads"
                )
            ).all()
        assert [tuple(r) for r in rows] == [("d1", "v2", 2, "legacy", "")]
        migrate.downgrade(url, "0002")
        assert "document_heads" not in _tables(engine)
        cols = {c["name"] for c in sa.inspect(engine).get_columns("document_versions")}
        assert "apply_state" not in cols
    finally:
        engine.dispose()


def test_0004_budget_tables_are_migrated_and_shared(tmp_path):
    """Revision 0004 owns the LLM budget tables: a store built with ``create_tables=False``
    works on a migrated database, two stores share one limit, and a database where an older
    ``SqlBudgetStore`` already created the tables lazily still upgrades."""
    from decimal import Decimal

    import pytest

    from jettae.llm.base import BudgetExceeded
    from jettae.llm.budget import Budget
    from jettae.llm.budget_store import SqlBudgetStore

    url = f"sqlite:///{tmp_path.as_posix()}/m4.db"
    migrate.upgrade(url)
    engine = make_engine(url)
    try:
        assert {"llm_budget", "llm_budget_entry"} <= _tables(engine)
        a = Budget(Decimal(10), store=SqlBudgetStore(url, create_tables=False))
        b = Budget(Decimal(10), store=SqlBudgetStore(url, create_tables=False))
        r = a.reserve(Decimal(8), model="m", purpose="test")
        with pytest.raises(BudgetExceeded):
            b.reserve(Decimal(8), model="m", purpose="test")
        a.settle(r, Decimal(8))
        migrate.downgrade(url, "0003")
        assert not {"llm_budget", "llm_budget_entry"} & _tables(engine)
    finally:
        engine.dispose()

    # tables created lazily (before this revision existed), then the migration runs
    url2 = f"sqlite:///{tmp_path.as_posix()}/m4b.db"
    migrate.upgrade(url2, "0003")
    SqlBudgetStore(url2).close()
    migrate.upgrade(url2)
    engine2 = make_engine(url2)
    try:
        assert migrate.current_revision(engine2) == migrate.head_revision()
    finally:
        engine2.dispose()
