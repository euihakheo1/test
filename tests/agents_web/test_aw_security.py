"""Investigations: tenant isolation (API and worker), roles, cookie sessions with CSRF,
job cancellation, migration head, and the startup gate of the worker / MCP commands."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from aw_helpers import API, Web, q, seed_conflict, tenant_of
from jt_api_helpers import cookie_header, login_session, signup
from typer.testing import CliRunner

from jettae.db import migrate
from jettae.db.orm import JobRow


def _analyzed(client: Any, rt: Any, work: Any, email: str) -> tuple[Web, str, str]:
    web = Web(client, signup(client, email, email.split("@")[0]))
    tenant = tenant_of(client, web.h)
    docs = seed_conflict(rt, tenant)
    web.analyze()
    work()
    return web, tenant, docs["settle_doc"]


def test_tenants_see_and_start_only_their_own_investigations(client, make_client, rt, work):
    alice, a_tenant, a_doc = _analyzed(client, rt, work, "alice@a.example")
    bob = Web(make_client(), signup(make_client(), "bob@b.example", "bob"))
    did = "dec:S1"  # alice's decision id; bob has no decisions yet

    r = alice.start(did, "single")
    assert r.status_code == 202
    a_inv = r.json()["investigation_id"]
    # bob cannot start on, list, or poll alice's decision / job
    assert bob.start(did).status_code == 404
    lst = bob.c.get(f"{API}/decisions/{q(did)}/investigations", headers=bob.h)
    assert lst.status_code == 404
    assert bob.c.get(f"{API}/jobs/{r.json()['job_id']}", headers=bob.h).status_code == 404

    # bob gets his own decision with the SAME id: still only his own investigations
    b_tenant = tenant_of(bob.c, bob.h)
    b_doc = seed_conflict(rt, b_tenant)["settle_doc"]
    bob.analyze()
    work()
    assert bob.investigations(did) == []
    b_inv = bob.start(did, "roles").json()["investigation_id"]
    work()
    a_view = alice.investigations(did)
    b_view = bob.investigations(did)
    assert [i["id"] for i in a_view] == [a_inv] and [i["id"] for i in b_view] == [b_inv]
    a_cited = {c["doc_version_id"] for f in a_view[0]["findings"] for c in f["citations"]}
    b_cited = {c["doc_version_id"] for f in b_view[0]["findings"] for c in f["citations"]}
    assert a_doc in a_cited and b_doc not in a_cited
    assert b_doc in b_cited and a_doc not in b_cited


def test_worker_never_runs_another_tenants_investigation(client, make_client, rt, work):
    alice, _, _ = _analyzed(client, rt, work, "alice2@a.example")
    a_inv = alice.start("dec:S1").json()["investigation_id"]
    bob = Web(make_client(), signup(make_client(), "bob2@b.example", "bob"))
    b_tenant = tenant_of(bob.c, bob.h)
    # a job of bob's tenant that names alice's investigation (e.g. a forged payload)
    now = rt.clock()
    forged = f"job_{uuid.uuid4().hex}"
    with rt.db.write(b_tenant) as s:
        s.add(
            JobRow(
                id=forged,
                tenant_id=b_tenant,
                type="investigate_decision",
                status="queued",
                payload={"investigation_id": a_inv},
                attempts=0,
                max_attempts=1,
                run_after=now,
                lease_version=0,
                cancel_requested=False,
                created_at=now,
                updated_at=now,
            )
        )
    with rt.db.write(b_tenant) as s:  # run the forged job first
        s.get(JobRow, forged).run_after = now.replace(year=2000)
    work()
    job = bob.job(forged)
    assert job["status"] == "failed" and job["error"]["code"] == "not_found"
    # alice's own job ran normally under her tenant
    (inv,) = alice.investigations("dec:S1")
    assert inv["status"] == "succeeded"


def test_viewer_can_read_but_not_start(client, rt, work):
    web, _, _ = _analyzed(client, rt, work, "owner@v.example")
    r = client.post(f"{API}/auth/api-tokens", headers=web.h, json={"name": "ro", "role": "viewer"})
    assert r.status_code == 201, r.text
    viewer = {"Authorization": f"Bearer {r.json()['token']}"}
    assert web.start("dec:S1", headers=viewer).status_code == 403
    assert client.get(f"{API}/decisions/{q('dec:S1')}/investigations", headers=viewer).json() == []
    caps = client.get(f"{API}/investigations/capabilities", headers=viewer)
    assert caps.status_code == 200


def test_cookie_session_needs_csrf_token_to_start(client, rt, work):
    web, _, _ = _analyzed(client, rt, work, "cookie@c.example")
    cookies = login_session(client, "cookie@c.example")
    url = f"{API}/decisions/{q('dec:S1')}/investigations"
    body = {"strategy": "single", "mode": "offline"}
    no_csrf = client.post(url, headers=cookie_header(cookies, csrf=False), json=body)
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "csrf_failed"
    wrong = {**cookie_header(cookies), "X-CSRF-Token": "not-the-cookie"}
    assert client.post(url, headers=wrong, json=body).status_code == 403
    ok = client.post(url, headers=cookie_header(cookies), json=body)
    assert ok.status_code == 202, ok.text
    listed = client.get(url, headers=cookie_header(cookies, csrf=False))  # GET: no CSRF
    assert listed.status_code == 200 and listed.json()[0]["id"] == ok.json()["investigation_id"]


def test_idempotent_start_and_cancelled_job_shows_failed(client, rt, work):
    web, _, _ = _analyzed(client, rt, work, "idem@i.example")
    key = {"Idempotency-Key": "inv-1"}
    first = web.start("dec:S1", headers=key)
    again = web.start("dec:S1", headers=key)
    assert first.status_code == again.status_code == 202
    assert again.json() == first.json() and again.headers.get("Idempotent-Replayed") == "true"
    assert len(web.investigations("dec:S1")) == 1
    cancel = client.post(f"{API}/jobs/{first.json()['job_id']}/cancel", headers=web.h)
    assert cancel.status_code == 202
    work()
    (inv,) = web.investigations("dec:S1")
    assert inv["status"] == "failed" and inv["error"]["code"] == "job_cancelled"


def test_investigation_table_is_in_the_migration_head(tmp_path):
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(migrate.alembic_config("sqlite://"))
    assert script.get_heads() == ["0006_investigations"]
    assert script.get_revision("0006_investigations").down_revision == "0005_auth_sessions"
    url = f"sqlite:///{tmp_path.as_posix()}/m.db"
    migrate.upgrade(url)
    import sqlalchemy as sa

    eng = sa.create_engine(url)
    try:
        assert "agent_investigations" in sa.inspect(eng).get_table_names()
        migrate.downgrade(url, "0005_auth_sessions")
        assert "agent_investigations" not in sa.inspect(eng).get_table_names()
    finally:
        eng.dispose()


# --------------------------------------------------------------------------- startup gate
@pytest.mark.parametrize(
    ("module", "args"),
    # each Typer app has one command, so it is invoked without the command name
    [("jettae.worker", ["--once"]), ("jettae.mcp_server", [])],
)
@pytest.mark.parametrize("env", ["production", "prod"])
def test_worker_and_mcp_refuse_bad_environment_before_opening_the_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str, args: list[str], env: str
):
    import importlib

    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    db = tmp_path / "never.db"
    monkeypatch.setenv("JETTAE_ENV_FILE", str(empty))
    monkeypatch.setenv("JETTAE_ENV", env)
    monkeypatch.setenv("JETTAE_DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.delenv("JETTAE_JWT_SECRET", raising=False)
    app = importlib.import_module(module).app
    res = CliRunner().invoke(app, args)
    assert res.exit_code != 0
    assert "configuration error" in res.output or "refusing to start" in res.output
    assert not db.exists()
