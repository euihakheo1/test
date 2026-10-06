from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio.from_thread
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from la_helpers import NOW, seed_tenant  # noqa: E402

from jettae.app import JettaeService, in_memory_repositories  # noqa: E402
from jettae.db import migrate  # noqa: E402
from jettae.db.config import Settings  # noqa: E402
from jettae.db.runtime import Runtime  # noqa: E402

T = "t1"
OTHER = "t2"


@pytest.fixture
def svc() -> JettaeService:
    s = JettaeService(in_memory_repositories(), clock=lambda: NOW)
    seed_tenant(s, T)
    seed_tenant(s, OTHER, prefix="B")
    return s


@pytest.fixture
def db_settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path.as_posix()}/la.db",
        blob_dir=tmp_path / "blobs",
        jwt_secret="test-secret-" + "y" * 40,
        argon2_time_cost=1,
        argon2_memory_kib=8192,
        argon2_parallelism=1,
    )


@pytest.fixture
def rt(db_settings: Settings) -> Iterator[Runtime]:
    migrate.upgrade(db_settings.database_url)
    r = Runtime.build(db_settings, clock=lambda: NOW)
    yield r
    r.close()


@pytest.fixture(scope="session")
def portal() -> Iterator[Any]:
    """One event loop for the session (a new asyncio loop per test intermittently failed on
    this Windows host with WinError 10014, see tests/api/conftest.py)."""
    with anyio.from_thread.start_blocking_portal() as p:
        yield p
