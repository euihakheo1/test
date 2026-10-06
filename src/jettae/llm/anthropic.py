"""Claude (Anthropic Messages API) provider. Optional extra ``llm``.

Structured output uses ``output_config={"format": {"type": "json_schema", ...}}``; images
are sent as base64 ``image`` blocks. The SDK's own retries are disabled (``max_retries=0``)
so that the gateway's limited retry + budget accounting sees every attempt.

The model comes from ``JETTAE_LLM_MODEL`` (default ``claude-opus-5-5``). Server-side refusal
fallbacks are deliberately not enabled: a fallback may be served by another model whose
price the budget has not reserved for; a refusal is reported as :class:`LLMRefusal`.
"""

from __future__ import annotations

import os
from typing import Any

from jettae.llm.base import (
    ImagePart,
    LLMError,
    LLMRequest,
    ProviderResponse,
    TokenUsage,
    TransientLLMError,
)

DEFAULT_MODEL = "claude-opus-5-5"


class AnthropicProvider:
    name = "anthropic"
    is_external = True
    supports_vision = True

    def __init__(self, model: str | None = None, *, client: Any = None) -> None:
        self.model = model or os.environ.get("JETTAE_LLM_MODEL") or DEFAULT_MODEL
        if client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover - depends on installed extras
                raise LLMError("the 'anthropic' package is not installed (extra: llm)") from e
            client = anthropic.Anthropic(max_retries=0)
        self._client = client

    @staticmethod
    def build_messages(request: LLMRequest) -> list[dict[str, Any]]:
        out = []
        for m in request.messages:
            content: list[dict[str, Any]] = []
            for p in m.parts:
                if isinstance(p, ImagePart):
                    content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": p.media_type,
                                "data": p.b64(),
                            },
                        }
                    )
                else:
                    content.append({"type": "text", "text": p.text})
            out.append({"role": m.role, "content": content})
        return out

    def complete(self, request: LLMRequest, *, timeout_s: float) -> ProviderResponse:
        import anthropic

        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=request.max_output_tokens,
                system=request.system,
                messages=self.build_messages(request),
                output_config={"format": {"type": "json_schema", "schema": dict(request.schema)}},
                timeout=timeout_s,
            )
        except anthropic.APITimeoutError as e:
            raise TransientLLMError(f"anthropic timeout: {e}", maybe_billed=True) from e
        except anthropic.APIConnectionError as e:
            raise TransientLLMError(f"anthropic connection error: {e}") from e
        except anthropic.RateLimitError as e:
            raise TransientLLMError(f"anthropic rate limited: {e}") from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientLLMError(
                    f"anthropic server error {e.status_code}", maybe_billed=True
                ) from e
            raise LLMError(f"anthropic error {e.status_code}: {e.message}") from e
        # refusal / max_tokens are returned (not raised) so the gateway can settle the cost
        stop = str(getattr(resp, "stop_reason", "") or "")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        u = resp.usage
        usage = TokenUsage(
            input_tokens=int(u.input_tokens or 0),
            output_tokens=int(u.output_tokens or 0),
            cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        )
        return ProviderResponse(
            text, usage, str(resp.model), stop, getattr(resp, "_request_id", None)
        )
