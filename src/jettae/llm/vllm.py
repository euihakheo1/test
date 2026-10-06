"""Self-hosted, text-only vLLM adapter; never routes to a paid OpenAI endpoint.

Only loopback addresses, private IP literals and the Compose service ``vllm`` are accepted.
Redirects and environment proxies are disabled: local mode must not forward tenant documents
to a public provider because OPENAI_BASE_URL or an HTTP_PROXY happens to be set. The operator
is responsible for keeping the private inference host under their control.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from jettae.llm.base import LLMError, LLMRequest, ProviderResponse, TokenUsage, TransientLLMError
from jettae.llm.openai import OpenAIProvider

DEFAULT_BASE_URL = "http://127.0.0.1:8001/v1"
DEFAULT_MODEL = "Qwen3-4B-Instruct-2507-cdbee75"


def validate_base_url(value: str) -> str:
    """Fail before opening a client; do not echo a possibly credential-bearing URL."""
    try:
        url = urlsplit(value)
        _ = url.port
        host = url.hostname or ""
        private = host in ("localhost", "vllm")
        try:
            ip = ipaddress.ip_address(host)
            networks = (
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "fc00::/7",
            )
            private = ip.is_loopback or any(ip in ipaddress.ip_network(n) for n in networks)
        except ValueError:
            pass
        valid = (
            url.scheme in ("http", "https")
            and private
            and url.username is None
            and url.password is None
            and url.path.rstrip("/") == "/v1"
            and not url.query
            and not url.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise LLMError(
            "JETTAE_VLLM_BASE_URL must be a private/loopback /v1 endpoint without "
            "URL credentials, query or fragment"
        )
    return value.rstrip("/")


def local_configuration_problem(env: Mapping[str, str]) -> str | None:
    if (env.get("JETTAE_LLM_MODE") or "offline").strip().lower() != "local":
        return "local inference is disabled by the server"
    if (env.get("JETTAE_LLM_PROVIDER") or "").strip().lower() != "vllm":
        return "JETTAE_LLM_MODE=local requires JETTAE_LLM_PROVIDER=vllm"
    try:
        validate_base_url(env.get("JETTAE_VLLM_BASE_URL") or DEFAULT_BASE_URL)
    except LLMError as exc:
        return str(exc)
    return None


class VLLMProvider:
    name = "vllm"
    is_external = False
    supports_vision = False

    def __init__(
        self, model: str | None = None, *, env: Mapping[str, str] | None = None, client: Any = None
    ) -> None:
        e = os.environ if env is None else env
        self.model = model or e.get("JETTAE_VLLM_MODEL") or DEFAULT_MODEL
        self.base_url = validate_base_url(e.get("JETTAE_VLLM_BASE_URL") or DEFAULT_BASE_URL)
        if client is None:
            import openai

            client = openai.OpenAI(
                base_url=self.base_url,
                api_key=e.get("JETTAE_VLLM_API_KEY") or "local-no-key",
                max_retries=0,
                http_client=openai.DefaultHttpxClient(trust_env=False, follow_redirects=False),
            )
        self._client = client

    def complete(self, request: LLMRequest, *, timeout_s: float) -> ProviderResponse:
        import openai

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=OpenAIProvider.build_messages(request),
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.schema_name,
                        "schema": dict(request.schema),
                        "strict": True,
                    },
                },
                max_tokens=request.max_output_tokens,
                temperature=0.1,
                timeout=timeout_s,
            )
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            raise TransientLLMError("vLLM endpoint unavailable or timed out") from exc
        except openai.APIStatusError as exc:
            # SDK error bodies can contain document text. Keep only the status in app logs.
            if exc.status_code == 429 or exc.status_code >= 500:
                raise TransientLLMError(f"vLLM endpoint returned HTTP {exc.status_code}") from exc
            raise LLMError(f"vLLM endpoint returned HTTP {exc.status_code}") from exc
        if not response.choices or response.usage is None:
            raise LLMError("vLLM response omitted choices or token usage")
        choice = response.choices[0]
        stop = "max_tokens" if choice.finish_reason == "length" else str(choice.finish_reason)
        if getattr(choice.message, "refusal", None):
            stop = "refusal"
        return ProviderResponse(
            choice.message.content or "",
            TokenUsage(response.usage.prompt_tokens, response.usage.completion_tokens),
            str(response.model),
            stop,
        )
