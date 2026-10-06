"""Environment contract: JETTAE_ENV in {dev,test,prod}, .env loading precedence and the prod
startup requirements of :func:`jettae.config.require_valid_environment`."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from jettae.config import (
    Settings,
    environment_problems,
    load_env,
    require_valid_environment,
)

STRONG = "Qm7-pX2rT9vL4kN8sB1cH6jD0fG5aE3wZ" + "uY"  # 35 bytes, not a placeholder
PG = "postgresql+psycopg://jettae@db.internal:5432/jettae"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Empty JETTAE_* environment and a fresh working directory. ``load_env`` writes into
    ``os.environ`` directly, so the whole environment is restored afterwards."""
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


def _prod(**over: object) -> None:
    base = {
        "JETTAE_ENV": "prod",
        "JETTAE_JWT_SECRET": STRONG,
        "JETTAE_DATABASE_URL": PG,
        "JETTAE_ALLOWED_ORIGINS": "https://app.example",
    }
    base.update({k: str(v) for k, v in over.items() if v is not None})
    for k, v in base.items():
        os.environ[k] = v
    for k, v in over.items():
        if v is None:
            os.environ.pop(k, None)


# ------------------------------------------------------------------ environment name
def test_default_is_dev(env):
    assert require_valid_environment("api").env == "dev"


@pytest.mark.parametrize("name", ["production", "PROD", "staging", "", "Dev "])
def test_invalid_environment_name_is_rejected(env, name):
    os.environ["JETTAE_ENV"] = name
    with pytest.raises(SystemExit) as e:
        require_valid_environment("worker")
    msg = str(e.value)
    assert "JETTAE_ENV must be one of dev, test, prod" in msg and repr(name) in msg
    with pytest.raises(ValidationError):
        Settings(env=name)


@pytest.mark.parametrize("name", ["dev", "test"])
def test_dev_and_test_have_no_prod_requirements(env, name):
    os.environ["JETTAE_ENV"] = name
    os.environ["JETTAE_LLM_MODE"] = "live"  # the budget gate refuses calls by itself
    for comp in ("api", "worker", "mcp", "db", "sources"):
        assert require_valid_environment(comp).env == name


# ------------------------------------------------------------------ prod requirements
def test_complete_prod_configuration_passes(env):
    _prod(JETTAE_DRF_OC="own" + secrets.token_hex(4))  # not the sample key "test"
    for comp in ("api", "worker", "mcp", "db", "sources"):
        st = require_valid_environment(comp)
        assert st.is_prod and st.secure_cookies


@pytest.mark.parametrize(
    ("secret", "fragment"),
    [
        (None, "is required"),
        ("x" * 31, "at least 32 bytes"),
        ("change-me-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3", "placeholder"),
        ("replace-with-a-random-secret-of-48-chars-0123456", "placeholder"),
        ("ab" * 20, "distinct characters"),
    ],
)
def test_prod_rejects_missing_or_weak_jwt_secret(env, secret, fragment):
    _prod(JETTAE_JWT_SECRET=secret)
    with pytest.raises(SystemExit) as e:
        require_valid_environment("api")
    assert fragment in str(e.value)
    if secret:
        assert secret not in str(e.value)  # never echo the secret
    # the worker does not sign tokens and is not blocked by it
    assert require_valid_environment("worker").is_prod


@pytest.mark.parametrize("url", ["sqlite:///./var/jettae.db", "mysql://u@h/db"])
def test_prod_rejects_non_postgres_database(env, url):
    _prod(JETTAE_DATABASE_URL=url)
    for comp in ("api", "worker", "mcp", "db"):
        with pytest.raises(SystemExit) as e:
            require_valid_environment(comp)
        assert "PostgreSQL" in str(e.value)


def test_prod_rejects_missing_or_insecure_origins(env):
    _prod(JETTAE_ALLOWED_ORIGINS=None)
    with pytest.raises(SystemExit) as e:
        require_valid_environment("api")
    assert "JETTAE_ALLOWED_ORIGINS" in str(e.value)
    _prod(JETTAE_ALLOWED_ORIGINS="http://app.example")
    with pytest.raises(SystemExit) as e:
        require_valid_environment("api")
    assert "https://" in str(e.value)


def test_wildcard_origin_is_rejected_in_every_environment(env):
    os.environ["JETTAE_ALLOWED_ORIGINS"] = "*"
    with pytest.raises(SystemExit) as e:
        require_valid_environment("api")
    assert "'*' is not allowed" in str(e.value)
    os.environ["JETTAE_ALLOWED_ORIGINS"] = "https://app.example/path"
    with pytest.raises(SystemExit):
        require_valid_environment("api")


def test_older_cors_variable_name_still_sets_allowed_origins(env):
    os.environ["JETTAE_CORS_ORIGINS"] = "http://localhost:3000, http://127.0.0.1:3000/"
    st = require_valid_environment("api")
    assert st.allowed_origins == ("http://localhost:3000", "http://127.0.0.1:3000")
    assert st.cors_origins == st.allowed_origins


def test_prod_rejects_disabled_secure_cookies(env):
    _prod(JETTAE_COOKIE_SECURE="false")
    with pytest.raises(SystemExit) as e:
        require_valid_environment("api")
    assert "JETTAE_COOKIE_SECURE" in str(e.value)
    # dev may turn it on explicitly (https dev proxy) or keep the default (off)
    assert Settings(env="dev").secure_cookies is False
    assert Settings(env="dev", cookie_secure=True).secure_cookies is True


@pytest.mark.parametrize("oc", [None, "test", " test ", ""])
def test_prod_source_commands_need_own_drf_oc(env, oc):
    _prod(JETTAE_DRF_OC=oc)
    if oc is not None:
        os.environ["JETTAE_DRF_OC"] = oc
    with pytest.raises(SystemExit) as e:
        require_valid_environment("sources:ftc")
    assert "JETTAE_DRF_OC" in str(e.value)
    # the API does not call law.go.kr and is not blocked by it
    assert require_valid_environment("api").is_prod


def test_prod_live_llm_needs_budget_and_shared_budget_db(env):
    _prod(JETTAE_LLM_MODE="live")
    with pytest.raises(SystemExit) as e:
        require_valid_environment("worker")
    msg = str(e.value)
    assert "JETTAE_LLM_BUDGET_KRW > 0" in msg and "JETTAE_LLM_BUDGET_DB" in msg
    _prod(JETTAE_LLM_MODE="live", JETTAE_LLM_BUDGET_KRW="5000")
    with pytest.raises(SystemExit) as e:
        require_valid_environment("api")
    assert "JETTAE_LLM_BUDGET_DB" in str(e.value) and "BUDGET_KRW" not in str(e.value)
    _prod(JETTAE_LLM_MODE="live", JETTAE_LLM_BUDGET_KRW="0", JETTAE_LLM_BUDGET_DB=PG)
    with pytest.raises(SystemExit):
        require_valid_environment("mcp")
    _prod(JETTAE_LLM_MODE="live", JETTAE_LLM_BUDGET_KRW="5000", JETTAE_LLM_BUDGET_DB=PG)
    assert require_valid_environment("worker").is_prod
    # offline / replay need no budget
    _prod(JETTAE_LLM_MODE="replay", JETTAE_LLM_BUDGET_KRW="0", JETTAE_LLM_BUDGET_DB=None)
    assert require_valid_environment("worker").is_prod


def test_all_prod_problems_are_reported_together(env):
    os.environ["JETTAE_ENV"] = "prod"
    problems = environment_problems(Settings(), "api", {})
    assert len(problems) == 3  # secret, database, origins
    with pytest.raises(SystemExit) as e:
        require_valid_environment("api")
    assert str(e.value).count("\n  - ") == 3


# ------------------------------------------------------------------ .env loading
def test_dotenv_in_cwd_is_loaded_and_process_env_wins(env):
    (env / ".env").write_text(
        "JETTAE_API_PORT=9001\nJETTAE_WORKER_CONCURRENCY=7\n# comment\nJETTAE_JWT_SECRET='a b'\n",
        encoding="utf-8",
    )
    os.environ["JETTAE_API_PORT"] = "9002"
    st = require_valid_environment("api")
    assert st.api_port == 9002  # real environment overrides the file
    assert st.worker_concurrency == 7  # only in the file
    assert st.jwt_secret == "a b"
    assert os.environ["JETTAE_WORKER_CONCURRENCY"] == "7"  # visible to os.environ readers


def test_env_file_variable_takes_precedence_over_cwd_dotenv(env):
    (env / ".env").write_text("JETTAE_API_PORT=1111\n", encoding="utf-8")
    other = env / "conf" / "prod.env"
    other.parent.mkdir()
    other.write_text("JETTAE_API_PORT=2222\nJETTAE_ENV=test\n", encoding="utf-8")
    os.environ["JETTAE_ENV_FILE"] = str(other)
    st = require_valid_environment("api")
    assert st.api_port == 2222 and st.env == "test"


def test_relative_env_file_resolves_against_cwd(env):
    (env / "local.env").write_text("JETTAE_API_PORT=3333\n", encoding="utf-8")
    os.environ["JETTAE_ENV_FILE"] = "local.env"
    assert require_valid_environment("api").api_port == 3333


def test_missing_env_file_is_an_error_not_a_silent_default(env):
    os.environ["JETTAE_ENV_FILE"] = str(env / "nope.env")
    with pytest.raises(SystemExit) as e:
        require_valid_environment("api")
    assert "missing file" in str(e.value)


def test_invalid_env_name_from_dotenv_is_rejected(env):
    (env / ".env").write_text("JETTAE_ENV=production\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        require_valid_environment("db")
    assert "'production'" in str(e.value)


def test_load_env_into_explicit_mapping(env):
    (env / ".env").write_text("A_KEY=file\nB_KEY=file\n", encoding="utf-8")
    target = {"A_KEY": "process"}
    assert load_env(target, env) == env / ".env"
    assert target == {"A_KEY": "process", "B_KEY": "file"}
    assert "B_KEY" not in os.environ


# ------------------------------------------------------------------ entry points
def test_api_factory_and_cli_refuse_invalid_environment(env):
    from typer.testing import CliRunner

    from jettae.api.cli import app as api_cli
    from jettae.api.main import app_factory

    os.environ["JETTAE_ENV"] = "production"
    with pytest.raises(SystemExit):
        app_factory()
    res = CliRunner().invoke(api_cli, ["migrate"])
    assert res.exit_code != 0
    assert "JETTAE_ENV must be one of" in (res.output + str(res.exception))


def test_api_factory_refuses_prod_without_secret(env):
    from jettae.api.main import app_factory

    _prod(JETTAE_JWT_SECRET=None)
    with pytest.raises(SystemExit) as e:
        app_factory()
    assert "JETTAE_JWT_SECRET" in str(e.value)


# ------------------------------------------------------------------ non-finite budgets, DB password
@pytest.mark.parametrize("raw", ["Infinity", "inf", "1e400", "NaN", "-5", "abc"])
def test_prod_live_budget_must_be_finite_and_positive(env, raw):
    # float("1e400") is inf and float("Infinity") > 0; an unlimited budget is no limit
    _prod(JETTAE_LLM_MODE="live", JETTAE_LLM_BUDGET_KRW=raw, JETTAE_LLM_BUDGET_DB=PG)
    for comp in ("api", "worker", "mcp"):
        with pytest.raises(SystemExit) as e:
            require_valid_environment(comp)
        assert "JETTAE_LLM_BUDGET_KRW > 0" in str(e.value)


@pytest.mark.parametrize("raw", ["Infinity", "inf", "1e400", "NaN", "-1"])
def test_gateway_and_budget_refuse_unbounded_limits(raw):
    from decimal import Decimal, InvalidOperation

    from jettae.llm.base import LLMError
    from jettae.llm.budget import Budget
    from jettae.llm.gateway import gateway_from_env

    env = {"JETTAE_LLM_MODE": "live", "JETTAE_LLM_BUDGET_KRW": raw}
    with pytest.raises(LLMError):
        gateway_from_env(env)
    try:
        limit = Decimal(raw)
    except InvalidOperation:
        return
    with pytest.raises(LLMError):
        Budget(limit)


def test_prod_rejects_placeholder_database_password(env):
    _prod(JETTAE_DATABASE_URL="postgresql+psycopg://jettae:change-me-db-password@db:5432/j")
    for comp in ("api", "worker", "mcp", "db"):
        with pytest.raises(SystemExit) as e:
            require_valid_environment(comp)
        msg = str(e.value)
        assert "JETTAE_DATABASE_URL contains a placeholder password" in msg
        assert "change-me-db-password" not in msg  # never echo the value
    real = secrets.token_urlsafe(24)
    _prod(JETTAE_DATABASE_URL="postgresql+psycopg://jettae:" + real + "@db:5432/j")
    assert require_valid_environment("db").is_prod
    # the shared LLM budget database is checked the same way
    _prod(
        JETTAE_LLM_MODE="live",
        JETTAE_LLM_BUDGET_KRW="5000",
        JETTAE_LLM_BUDGET_DB="postgresql+psycopg://jettae:example-pass@db:5432/j",
    )
    with pytest.raises(SystemExit) as e:
        require_valid_environment("worker")
    assert "JETTAE_LLM_BUDGET_DB contains a placeholder password" in str(e.value)
