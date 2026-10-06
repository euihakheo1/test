"""Actual local inference smoke check. No cached answer, no claim about document accuracy."""

from __future__ import annotations

import json

from jettae.config import require_valid_environment
from jettae.llm.base import LLMMessage, LLMRequest, strict_object
from jettae.llm.gateway import LLMMode, gateway_from_env


def main() -> None:
    require_valid_environment("worker")
    gateway = gateway_from_env()
    if gateway.config.mode is not LLMMode.LOCAL:
        raise SystemExit("This check requires JETTAE_LLM_MODE=local; paid providers are refused")
    request = LLMRequest(
        purpose="local.connection_check",
        system='Return the JSON object {"status":"ok"}.',
        messages=(LLMMessage.user("Check structured JSON output."),),
        schema=strict_object({"status": {"type": "string", "enum": ["ok"]}}),
        tenant_id="public",
        contains_tenant_data=False,
        prompt_id="local.connection_check",
        max_output_tokens=64,
    )
    result = gateway.complete(request)
    if result.from_cache:
        raise SystemExit("A replay was used; set JETTAE_LLM_LOCAL_CACHE=0 and repeat")
    print(
        json.dumps(
            {
                "model": result.model,
                "status": result.data["status"],
                "from_cache": result.from_cache,
                "usage": result.usage.to_json(),
                "latency_ms": result.latency_ms,
                "api_charge_krw": str(result.cost_krw),
            }
        )
    )


if __name__ == "__main__":
    main()
