"""Integration fixtures: real parser (``ModuleIngest`` -> ``jettae.ingest.pipeline``), real
worker, real SQLite database migrated with Alembic, FastAPI test client.

Every file used here is a tiny hand-written fixture (not evaluation data)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio.from_thread
import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "api"))

from fastapi.testclient import TestClient  # noqa: E402
from jt_api_helpers import FakeClock, signup  # noqa: E402

from jettae.api.main import create_app  # noqa: E402
from jettae.db import migrate  # noqa: E402
from jettae.db.config import Settings  # noqa: E402
from jettae.db.ingest_bridge import ModuleIngest  # noqa: E402
from jettae.db.runtime import Runtime  # noqa: E402
from jettae.worker import Worker  # noqa: E402

API = "/api/v1"


@pytest.fixture(scope="session")
def portal() -> Iterator[Any]:
    with anyio.from_thread.start_blocking_portal() as p:
        yield p


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        database_url=f"sqlite:///{tmp_path.as_posix()}/i.db",
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
def client(rt: Runtime, portal: Any) -> TestClient:
    tc = TestClient(create_app(rt))
    tc.portal = portal
    return tc


@pytest.fixture
def h(client: TestClient) -> dict[str, str]:
    return signup(client, "int@x.example", "통합 회사")


@pytest.fixture
def tenant(client: TestClient, h: dict[str, str]) -> str:
    return str(client.get(f"{API}/auth/me", headers=h).json()["tenant_id"])


@pytest.fixture
def work(rt: Runtime) -> Any:
    w = Worker(rt, owner="int-worker", concurrency=2, heartbeat_s=0.05)

    def run() -> int:
        n = 0
        while True:
            done = w.run_once()
            n += done
            if not done:
                return n

    return run


class Api:
    """Small client wrapper so the tests read like the user flow."""

    def __init__(self, client: TestClient, h: dict[str, str]) -> None:
        self.c, self.h = client, h

    def upload(
        self,
        text: str | bytes,
        *,
        kind: str = "settlement",
        document_id: str | None = None,
        name: str = "s.csv",
        auto_ingest: bool = True,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": kind, "auto_ingest": str(auto_ingest).lower()}
        if document_id:
            data["document_id"] = document_id
        content = text.encode() if isinstance(text, str) else text
        r = self.c.post(
            f"{API}/documents",
            headers=self.h,
            files={"file": (name, content, "text/csv")},
            data=data,
        )
        assert r.status_code == 201, r.text
        return dict(r.json())

    def job(self, job_id: str) -> dict[str, Any]:
        return dict(self.c.get(f"{API}/jobs/{job_id}", headers=self.h).json())

    def ingest(self, dvid: str, mapping: dict[str, Any] | None = None) -> str:
        params: dict[str, Any] = {"doc_version_id": dvid}
        if mapping is not None:
            params["mapping"] = mapping
        r = self.c.post(
            f"{API}/jobs", headers=self.h, json={"type": "ingest_document", "params": params}
        )
        assert r.status_code == 202, r.text
        return str(r.json()["job_id"])

    def confirm_mapping(self, dvid: str, mapping: dict[str, Any]) -> Any:
        return self.c.post(
            f"{API}/document-versions/{dvid}/mapping", headers=self.h, json={"mapping": mapping}
        )

    def analyze(self) -> str:
        r = self.c.post(
            f"{API}/jobs",
            headers=self.h,
            json={"type": "run_analysis", "params": {"as_of": "2025-11-01"}},
        )
        assert r.status_code == 202, r.text
        return str(r.json()["job_id"])

    def decision(self, record_id: str) -> dict[str, Any]:
        r = self.c.get(f"{API}/decisions/{quote('dec:' + record_id, safe='')}", headers=self.h)
        assert r.status_code == 200, r.text
        return dict(r.json())

    def approve(self, record_id: str, result_hash: str) -> Any:
        return self.c.post(
            f"{API}/decisions/{quote('dec:' + record_id, safe='')}/approvals",
            headers=self.h,
            json={"expected_result_hash": result_hash},
        )

    def version(self, dvid: str) -> dict[str, Any]:
        return dict(self.c.get(f"{API}/document-versions/{dvid}", headers=self.h).json())

    def versions(self, document_id: str) -> dict[str, Any]:
        return dict(self.c.get(f"{API}/documents/{document_id}/versions", headers=self.h).json())

    def acknowledge(self, dvid: str, fingerprint: str) -> Any:
        return self.c.post(
            f"{API}/document-versions/{dvid}/acknowledge",
            headers=self.h,
            json={"fingerprint": fingerprint},
        )


@pytest.fixture
def api(client: TestClient, h: dict[str, str]) -> Api:
    return Api(client, h)
