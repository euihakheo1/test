"""OpenAI Chat Completions provider. Optional extra ``llm``.

Structured output uses ``response_format={"type": "json_schema", "strict": true}``; images
are sent as ``image_url`` data URLs. SDK retries are disabled (the gateway retries).
Model: ``JETTAE_OPENAI_MODEL`` (required -- no default model is assumed). OpenAI prices are
not built into :mod:`jettae.llm.budget`; set them with ``JETTAE_LLM_PRICES``.
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


class OpenAIProvider:
    name = "openai"
    is_external = True
    supports_vision = True

    def __init__(self, model: str | None = None, *, client: Any = None) -> None:
        m = model or os.environ.get("JETTAE_OPENAI_MODEL")
        if not m:
            raise LLMError("set JETTAE_OPENAI_MODEL to use the OpenAI provider")
        self.model = m
        if client is None:
            try:
                import openai
            except ImportError as e:  # pragma: no cover - depends on installed extras
                raise LLMError("the 'openai' package is not installed (extra: llm)") from e
            client = openai.OpenAI(max_retries=0)
        self._client = client

    @staticmethod
    def build_messages(request: LLMRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
        for m in request.messages:
            content: list[dict[str, Any]] = []
            for p in m.parts:
                if isinstance(p, ImagePart):
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{p.media_type};base64,{p.b64()}"},
                        }
                    )
                else:
                    content.append({"type": "text", "text": p.text})
            out.append({"role": m.role, "content": content})
        return out

    def complete(self, request: LLMRequest, *, timeout_s: float) -> ProviderResponse:
        import openai

        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=self.build_messages(request),
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.schema_name,
                        "schema": dict(request.schema),
                        "strict": True,
                    },
                },
                max_completion_tokens=request.max_output_tokens,
                timeout=timeout_s,
            )
        except openai.APITimeoutError as e:
            raise TransientLLMError(f"openai timeout: {e}", maybe_billed=True) from e
        except openai.APIConnectionError as e:
            raise TransientLLMError(f"openai connection error: {e}") from e
        except openai.RateLimitError as e:
            raise TransientLLMError(f"openai rate limited: {e}") from e
        except openai.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientLLMError(
                    f"openai server error {e.status_code}", maybe_billed=True
                ) from e
            raise LLMError(f"openai error {e.status_code}: {e.message}") from e
        choice = resp.choices[0]
        # refusal / truncation are returned (not raised) so the gateway can settle the cost
        refusal = getattr(choice.message, "refusal", None)
        stop = "refusal" if refusal else str(choice.finish_reason or "")
        if stop == "length":
            stop = "max_tokens"
        u = resp.usage
        # cached prompt tokens are billed at a discount whose rate is not modelled here;
        # all prompt tokens are counted at the full input price (conservative).
        usage = TokenUsage(
            input_tokens=int(u.prompt_tokens or 0),
            output_tokens=int(u.completion_tokens or 0),
        )
        return ProviderResponse(
            str(refusal) if refusal else (choice.message.content or ""),
            usage,
            str(resp.model),
            stop,
            getattr(resp, "_request_id", None),
        )
