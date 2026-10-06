"""``jettae agent run`` and ``jettae mcp serve`` (stdio subprocess) against a SQLite DB."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from la_helpers import seed_tenant
from typer.testing import CliRunner

from jettae.api.auth import AuthService
from jettae.cli import app as root

PASSWORD = "correct horse battery"


@pytest.fixture
def env(rt, db_settings, tmp_path: Path, monkeypatch) -> dict[str, Any]:
    auth = AuthService(rt.db, rt.settings, clock=rt.clock)
    p = auth.authenticate(auth.signup("cli@example.com", PASSWORD, "CLI상사").access_token)
    _, token = auth.create_api_token(p, "cli", "member", None)
    seed_tenant(rt.service, p.tenant_id)
    values = {
        "JETTAE_ENV": "test",
        "JETTAE_DATABASE_URL": db_settings.database_url,
        "JETTAE_BLOB_DIR": str(db_settings.blob_dir),
        "JETTAE_JWT_SECRET": db_settings.jwt_secret or "",
        "JETTAE_AGENT_DIR": str(tmp_path / "agent"),
        "JETTAE_LLM_CACHE_DIR": str(tmp_path / "llm_cache"),
        "JETTAE_LLM_MODE": "offline",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("JETTAE_API_TOKEN", raising=False)
    monkeypatch.delenv("JETTAE_LLM_BUDGET_KRW", raising=False)
    return {"token": token, "tenant": p.tenant_id, "env": values, "tmp": tmp_path}


def run(*args: str) -> Any:
    return CliRunner().invoke(root, list(args))


def test_agent_run_heuristic_single_and_roles_same_numbers(env):
    outs = {}
    for strategy in ("single", "roles"):
        r = run(
            "agent",
            "run",
            "--strategy",
            strategy,
            "--decision",
            "dec:I1",
            "--planner",
            "heuristic",
            "--token",
            env["token"],
            "--json",
        )
        assert r.exit_code == 0, r.output
        outs[strategy] = json.loads(r.output)
    assert outs["single"]["engine"] == outs["roles"]["engine"]
    assert outs["single"]["status"] == "COMPLETED"
    run_file = Path(env["env"]["JETTAE_AGENT_DIR"])
    assert list(run_file.rglob(f"{outs['roles']['run_id']}.json"))
    # the 'agents' group (root CLI registration) is the same command
    r = run(
        "agent", "run", "--decision", "dec:I2", "--planner", "heuristic", "--token", env["token"]
    )
    assert r.exit_code == 0, r.output


def test_agent_run_replay_without_recording_is_blocked(env):
    r = run(
        "agent",
        "run",
        "--decision",
        "dec:I1",
        "--mode",
        "replay",
        "--token",
        env["token"],
        "--json",
    )
    assert r.exit_code == 2, r.output
    rep = json.loads(r.output)
    assert rep["status"] == "BLOCKED" and rep["engine"]["decision_id"] == "dec:I1"


def test_agent_run_live_without_budget_and_bad_token(env):
    r = run("agent", "run", "--decision", "dec:I1", "--mode", "live", "--token", env["token"])
    assert r.exit_code == 2 and "BUDGET" in r.output
    r = run(
        "agent", "run", "--decision", "dec:I1", "--planner", "heuristic", "--token", "jtk_wrong"
    )
    assert r.exit_code == 2
    r = run(
        "agent",
        "run",
        "--decision",
        "dec:nope",
        "--planner",
        "heuristic",
        "--token",
        env["token"],
        "--json",
    )
    assert r.exit_code == 3 and json.loads(r.output)["status"] == "FAILED"


def test_mcp_stdio_server_process(env, portal):
    from mcp import Client, StdioServerParameters

    child_env = {
        **os.environ,
        **env["env"],
        "JETTAE_API_TOKEN": env["token"],
        "PYTHONIOENCODING": "utf-8",
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "jettae.cli", "mcp", "serve"], env=child_env
    )

    async def go() -> Any:
        async with Client(params, read_timeout_seconds=60) as c:
            tools = await c.list_tools()
            res = await c.call_tool(
                "calculate_due", {"decision_id": "dec:I1", "tenant_id": "someone-else"}
            )
            miss = await c.call_tool("calculate_due", {"decision_id": "dec:BI1"})
            return tools, res, miss

    tools, res, miss = portal.call(go)
    assert len(tools.tools) == 10
    assert not res.is_error and res.structured_content["decision_id"] == "dec:I1"
    assert miss.is_error and "not_found" in miss.content[0].text
