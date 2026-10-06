"""The shipped example files: the local one starts as is, the production and live-LLM ones are
refused until their fake values are replaced, and none of them can switch on a paid call by
being copied unchanged."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import dotenv_values

from jettae.config import REPO_ROOT, require_valid_environment

EXAMPLES = (".env.example", ".env.live-llm.example", ".env.prod.example")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    saved = dict(os.environ)
    for k in list(os.environ):
        if k.upper().startswith(("JETTAE_", "ANTHROPIC_", "OPENAI_")):
            del os.environ[k]
    monkeypatch.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _use(env: Path, name: str) -> None:
    os.environ["JETTAE_ENV_FILE"] = str(REPO_ROOT / name)


def test_local_example_starts_every_component(env):
    _use(env, ".env.example")
    for comp in ("api", "worker", "mcp", "db", "sources:ftc"):
        st = require_valid_environment(comp)
        assert st.env == "dev"
    assert os.environ["JETTAE_LLM_MODE"] == "offline"
    assert os.environ["JETTAE_LLM_BUDGET_KRW"] == "0"


def test_prod_example_is_refused_until_secrets_are_replaced(env):
    _use(env, ".env.prod.example")
    with pytest.raises(SystemExit) as e:
        require_valid_environment("api")
    msg = str(e.value)
    assert "JETTAE_JWT_SECRET" in msg and "placeholder" in msg
    # the refusal names the setting, never its value
    assert "change-me-generate" not in msg
    with pytest.raises(SystemExit) as e:
        require_valid_environment("sources:ftc")  # the OC is still the example value
    assert "JETTAE_DRF_OC" in str(e.value)


def test_prod_example_passes_once_real_values_are_set(env):
    _use(env, ".env.prod.example")
    os.environ["JETTAE_JWT_SECRET"] = "Qm7-pX2rT9vL4kN8sB1cH6jD0fG5aE3wZuY"  # secret-scan: allow
    for comp in ("api", "worker", "mcp", "db"):
        assert require_valid_environment(comp).env == "prod"


def test_live_llm_example_cannot_call_a_model_unchanged(env):
    from jettae.llm.base import LLMError
    from jettae.llm.gateway import gateway_from_env

    values = {k: v or "" for k, v in dotenv_values(REPO_ROOT / ".env.live-llm.example").items()}
    assert values["JETTAE_LLM_MODE"] == "live"
    with pytest.raises(LLMError):
        gateway_from_env(values)


@pytest.mark.parametrize("name", EXAMPLES)
def test_examples_are_publishable_and_values_are_fake(name):
    path = REPO_ROOT / name
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", name], capture_output=True
    )
    assert out.returncode == 1, f"{name} is ignored by .gitignore"
    for key, value in dotenv_values(path).items():
        if any(w in key for w in ("SECRET", "API_KEY", "PASSWORD", "DRF_OC")) and value:
            assert value == "test" or "change-me" in value, key
        if key == "JETTAE_DATABASE_URL" and value and "@" in value:
            assert "change-me" in value, key


def test_real_env_files_are_ignored():
    for name in (".env", ".env.live-llm", ".env.prod", "frontend/.env.local", "var/x.db"):
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", name], capture_output=True
        )
        assert out.returncode == 0, f"{name} is not ignored"
