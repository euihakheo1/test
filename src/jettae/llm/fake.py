"""Deterministic in-process provider for tests and demos. Never leaves the process.

``FakeProvider(responder)``: ``responder(request) -> dict | str`` produces the JSON object
(or raw text) for each call; a list of responses is served in order. ``failures`` injects
exceptions before the n-th calls (e.g. a :class:`TransientLLMError` to test retry).
Token usage is a deterministic estimate (UTF-8 bytes / 4), so budget code paths run.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from jettae.llm.base import (
    LLMRequest,
    ProviderResponse,
    TextPart,
    TokenUsage,
)

Responder = Callable[[LLMRequest], Mapping[str, Any] | str]


class FakeProvider:
    name = "fake"
    is_external = False
    supports_vision = True

    def __init__(
        self,
        responder: Responder | Iterable[Mapping[str, Any] | str],
        *,
        model: str = "fake-model-1",
        failures: Mapping[int, Exception] | None = None,
        is_external: bool = False,
    ) -> None:
        self.model = model
        self.is_external = is_external
        if callable(responder):
            self._responder: Responder = responder
        else:
            queue = list(responder)

            def _next(_: LLMRequest) -> Mapping[str, Any] | str:
                if not queue:
                    raise AssertionError("FakeProvider: no scripted response left")
                return queue.pop(0)

            self._responder = _next
        self._failures = dict(failures or {})
        self.calls: list[LLMRequest] = []
        self.timeouts: list[float] = []

    def complete(self, request: LLMRequest, *, timeout_s: float) -> ProviderResponse:
        self.timeouts.append(timeout_s)
        n = len(self.timeouts)  # 1-based attempt number (failed attempts included)
        if n in self._failures:
            raise self._failures.pop(n)
        self.calls.append(request)
        out = self._responder(request)
        raw = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        text_in = sum(
            len(p.text.encode("utf-8"))
            for m in request.messages
            for p in m.parts
            if isinstance(p, TextPart)
        ) + len(request.system.encode("utf-8"))
        usage = TokenUsage(
            input_tokens=max(1, text_in // 4) + 1000 * request.image_count,
            output_tokens=max(1, len(raw.encode("utf-8")) // 4),
        )
        return ProviderResponse(raw, usage, self.model, "end_turn", f"fake-{n}")
