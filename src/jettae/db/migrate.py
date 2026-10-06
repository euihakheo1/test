"""Programmatic Alembic helpers (``alembic.ini`` at the repo root uses the same scripts)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from jettae.db.config import REPO_ROOT


def target_metadata() -> list[sa.MetaData]:
    """Everything the migrations own: the service ORM tables plus the shared LLM budget
    tables, which ``jettae.llm.budget_store`` defines in its own MetaData (revision 0004).
    ``orm_auth`` and ``orm_investigations`` register ``auth_sessions`` (0005) and
    ``agent_investigations`` (0006) on the same metadata only when imported, so both are
    imported here; otherwise a schema diff would report those tables as unknown extras."""
    import jettae.db.orm_auth  # noqa: F401
    import jettae.db.orm_investigations  # noqa: F401
    from jettae.db.orm import metadata
    from jettae.llm.budget_store import budget_tables

    return [metadata, budget_tables()[0]]


def alembic_config(url: str, migrations_dir: Path | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir or REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logging"] = False
    return cfg


def upgrade(url: str, revision: str = "head", migrations_dir: Path | None = None) -> None:
    command.upgrade(alembic_config(url, migrations_dir), revision)


def downgrade(url: str, revision: str = "base", migrations_dir: Path | None = None) -> None:
    command.downgrade(alembic_config(url, migrations_dir), revision)


@lru_cache(maxsize=8)
def head_revision(migrations_dir: Path | None = None) -> str | None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://", migrations_dir))
    return script.get_current_head()


def current_revision(engine: sa.Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
