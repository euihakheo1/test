"""Fixtures for the Agent-investigation web flow: real parser (``ModuleIngest``), real worker,
SQLite migrated with Alembic, FastAPI test client. Files are tiny hand-written fixtures (not
evaluation data)."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anyio.from_thread
import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "api"))

from fastapi.testclient import TestClient  # noqa: E402
from jt_api_helpers import FakeClock  # noqa: E402

from jettae.api.main import create_app  # noqa: E402
from jettae.db import migrate  # noqa: E402
from jettae.db.config import Settings  # noqa: E402
from jettae.db.ingest_bridge import ModuleIngest  # noqa: E402
from jettae.db.runtime import Runtime  # noqa: E402
from jettae.worker import DEFAULT_HANDLERS, Worker  # noqa: E402

LLM_VARS = (
    "JETTAE_LLM_MODE",
    "JETTAE_LLM_BUDGET_KRW",
    "JETTAE_LLM_BUDGET_DB",
    "JETTAE_LLM_LEDGER",
    "JETTAE_LLM_CACHE_DIR",
    "JETTAE_ENV",
)


@pytest.fixture(autouse=True)
def _no_server_llm_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paid calls are a server setting: start every test from 'not configured'."""
    for v in LLM_VARS:
        monkeypatch.delenv(v, raising=False)


@pytest.fixture(scope="session")
def portal() -> Iterator[Any]:
    with anyio.from_thread.start_blocking_portal() as p:
        yield p


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path.as_posix()}/aw.db",
        blob_dir=tmp_path / "blobs",
        jwt_secret="test-secret-" + "x" * 40,
        argon2_time_cost=1,
        argon2_memory_kib=8192,
        argon2_parallelism=1,
        max_upload_bytes=64 * 1024,
        max_json_body_bytes=256 * 1024,
        job_backoff_base_s=0.0,
        job_lease_s=30,
        job_max_attempts=3,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def rt(settings: Settings, clock: FakeClock) -> Iterator[Runtime]:
    migrate.upgrade(settings.database_url)
    r = Runtime.build(settings, clock=clock, ingest=ModuleIngest())
    yield r
    r.close()


@pytest.fixture
def make_client(portal: Any, rt: Runtime) -> Callable[[], TestClient]:
    def make() -> TestClient:
        tc = TestClient(create_app(rt))
        tc.portal = portal
        return tc

    return make


@pytest.fixture
def client(make_client: Callable[[], TestClient]) -> TestClient:
    return make_client()


@pytest.fixture
def work(rt: Runtime) -> Callable[..., int]:
    """Run the worker until no job is claimable. ``handlers`` overrides job handlers (e.g. an
    investigation handler with a test gateway)."""

    def run(handlers: dict[str, Any] | None = None) -> int:
        w = Worker(
            rt,
            owner="aw-worker",
            concurrency=2,
            heartbeat_s=0.05,
            handlers={**DEFAULT_HANDLERS, **(handlers or {})},
        )
        n = 0
        while True:
            done = w.run_once()
            n += done
            if not done:
                return n

    return run
