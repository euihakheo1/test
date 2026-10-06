"""Static checks of the CI workflow and the Compose file: properties that only show up when
CI runs on GitHub (which these tests cannot do), so a later edit cannot silently drop them."""

from __future__ import annotations

from typing import Any

import yaml

from jettae.config import REPO_ROOT

ASSERT_STEP = "Assert paid LLM calls are off"


def _ci() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))


def test_every_job_first_asserts_that_paid_calls_are_off() -> None:
    jobs = _ci()["jobs"]
    assert set(jobs) >= {"backend", "postgres", "frontend", "e2e"}
    for name, job in jobs.items():
        first = job["steps"][0]
        assert first.get("name") == ASSERT_STEP, name
        assert "ANTHROPIC_API_KEY" in first["run"] and "OPENAI_API_KEY" in first["run"], name


def test_piped_steps_fail_when_the_first_command_fails() -> None:
    # GitHub's default `bash -e {0}` has no pipefail: `pytest | tee log` would report tee's 0
    for name, job in _ci()["jobs"].items():
        default_shell = (job.get("defaults") or {}).get("run", {}).get("shell")
        for step in job["steps"]:
            run = step.get("run") or ""
            if "| tee" not in run and "|tee" not in run:
                continue
            shell = step.get("shell") or default_shell
            assert shell == "bash" or "set -o pipefail" in run, (name, step.get("name"))


def test_no_job_reads_repository_secrets_or_enables_live_llm() -> None:
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert "secrets." not in text
    env = _ci()["env"]
    assert env["JETTAE_LLM_MODE"] == "offline" and str(env["JETTAE_LLM_BUDGET_KRW"]) == "0"


def test_compose_api_trusts_only_its_network_gateway_for_forwarded_headers() -> None:
    compose = yaml.safe_load((REPO_ROOT / "deploy" / "docker-compose.yml").read_text("utf-8"))
    api_env = compose["services"]["api"]["environment"]
    gateway = compose["networks"]["default"]["ipam"]["config"][0]["gateway"]
    assert api_env["FORWARDED_ALLOW_IPS"] == gateway
    assert api_env["JETTAE_ENV"] == "prod"  # the merged app environment is kept
    assert "*" not in api_env["FORWARDED_ALLOW_IPS"]


def test_private_inference_has_no_public_port_and_the_proxy_is_explicitly_trusted() -> None:
    root = REPO_ROOT / "deploy"
    inference = yaml.safe_load((root / "compose.vllm.yml").read_text("utf-8"))
    services = inference["services"]
    assert not services["vllm"].get("ports")
    assert services["worker"]["environment"]["JETTAE_VLLM_BASE_URL"] == "http://vllm:8001/v1"
    web = yaml.safe_load((root / "compose.web.yml").read_text("utf-8"))
    proxy_ip = web["services"]["proxy"]["networks"]["default"]["ipv4_address"]
    assert web["services"]["api"]["environment"]["FORWARDED_ALLOW_IPS"] == proxy_ip
