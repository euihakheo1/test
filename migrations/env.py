"""Alembic environment. URL precedence: ``-x url=...`` > ``sqlalchemy.url`` in the config
> ``JETTAE_DATABASE_URL`` (via jettae Settings). ``render_as_batch`` keeps ALTERs working on
SQLite; the same scripts run on PostgreSQL."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from jettae.db.migrate import target_metadata as _target_metadata
from jettae.db.session import make_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name)

# ORM tables + LLM budget tables (see jettae.db.migrate.target_metadata)
target_metadata = _target_metadata()


def _url() -> str:
    x = context.get_x_argument(as_dictionary=True)
    if x.get("url"):
        return x["url"]
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from jettae.db.config import Settings

    return Settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is not None:
        _run(connectable)
        return
    engine = make_engine(_url())
    with engine.connect() as connection:
        _run(connection)
    engine.dispose()


def _run(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
