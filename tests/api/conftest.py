from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anyio.from_thread
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402
from jt_api_helpers import FakeClock, FakeIngest, signup  # noqa: E402

from jettae.api.main import create_app  # noqa: E402
from jettae.db import migrate  # noqa: E402
from jettae.db.config import Settings  # noqa: E402
from jettae.db.runtime import Runtime  # noqa: E402
from jettae.worker import Worker  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path.as_posix()}/t.db",
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
def ingest() -> FakeIngest:
    return FakeIngest()


@pytest.fixture
def rt(settings: Settings, clock: FakeClock, ingest: FakeIngest) -> Iterator[Runtime]:
    migrate.upgrade(settings.database_url)
    r = Runtime.build(settings, clock=clock, ingest=ingest)
    yield r
    r.close()


@pytest.fixture(scope="session")
def portal() -> Iterator[Any]:
    """One event loop for all TestClients of the session. Creating a new loop per client
    (asyncio self-pipe socketpair) intermittently failed on this Windows host with
    WinError 10014. Sharing is safe: the tests never enter the TestClient context, so the
    app lifespan (a settings check only) does not run."""
    with anyio.from_thread.start_blocking_portal() as p:
        yield p


@pytest.fixture
def make_client(portal: Any) -> Callable[[Runtime], TestClient]:
    def make(runtime: Runtime) -> TestClient:
        tc = TestClient(create_app(runtime))
        tc.portal = portal
        return tc

    return make


@pytest.fixture
def client(rt: Runtime, make_client: Callable[[Runtime], TestClient]) -> TestClient:
    return make_client(rt)


@pytest.fixture
def worker(rt: Runtime) -> Worker:
    return Worker(rt, owner="test-worker", concurrency=2, heartbeat_s=0.05)


@pytest.fixture
def alice(client: TestClient) -> dict[str, str]:
    return signup(client, "alice@a.example", "가 회사")


@pytest.fixture
def bob(client: TestClient) -> dict[str, str]:
    return signup(client, "bob@b.example", "나 회사")
