"""The test run ignores the developer's shell: README's live-LLM steps export
``JETTAE_ENV_FILE=.env.live-llm``, and a test run in that terminal must neither read that file
nor see a provider key (tests/_plugins/jettae_testenv.py)."""

from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner


def test_no_provider_key_or_live_setting_reaches_the_tests() -> None:
    assert not [k for k in os.environ if k.upper().startswith(("ANTHROPIC_", "OPENAI_"))]
    assert os.environ["JETTAE_LLM_MODE"] == "offline"
    assert os.environ["JETTAE_LLM_BUDGET_KRW"] == "0"
    env_file = Path(os.environ["JETTAE_ENV_FILE"])
    assert env_file.is_file() and env_file.stat().st_size == 0


def test_cli_ignores_a_dotenv_in_the_working_directory(tmp_path, monkeypatch) -> None:
    from jettae.cli import app

    (tmp_path / ".env").write_text("JETTAE_ENV=staging\nJETTAE_LLM_MODE=live\n", "utf-8")
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(app, ["rules", "list"])
    assert r.exit_code == 0, r.output
    assert os.environ["JETTAE_LLM_MODE"] == "offline" and "JETTAE_ENV" not in os.environ
