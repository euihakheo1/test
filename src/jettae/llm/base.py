"""LLM provider contract: structured JSON output validated against a JSON schema.

A provider turns an :class:`LLMRequest` into a :class:`ProviderResponse` (raw JSON text +
token usage). Everything else -- offline/replay/live/local mode, budget reservation, tenant
"external LLM" consent, schema validation, limited retry -- lives in
:class:`jettae.llm.gateway.LLMGateway`, so every provider gets the same policy.

Model output is never a number source for the engine: callers use it to pick tools,
read tables or draft text, and the engine recomputes every amount and date.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from jettae.domain.hashing import content_hash

Role = Literal["user", "assistant"]


# ----------------------------------------------------------------------------- errors
class LLMError(RuntimeError):
    """Non-retryable LLM failure (bad request, auth, refusal, truncated output...)."""

    code = "llm_error"


class TransientLLMError(LLMError):
    """Retryable failure (rate limit, overload, timeout, connection).

    ``maybe_billed``: the request may have reached the model (timeout / 5xx) -- the budget
    then keeps the reserved amount as spent, because the actual cost is unknown."""

    code = "llm_transient"

    def __init__(self, message: str, *, maybe_billed: bool = False) -> None:
        super().__init__(message)
        self.maybe_billed = maybe_billed


class LLMRefusal(LLMError):
    code = "llm_refusal"


class SchemaValidationError(LLMError):
    code = "llm_schema_invalid"

    def __init__(self, message: str, *, raw_text: str = "") -> None:
        super().__init__(message)
        self.raw_text = raw_text


class ReplayMiss(LLMError):
    """Offline/replay mode and no recorded response for this exact request."""

    code = "llm_replay_miss"


class LiveCallRefused(LLMError):
    """A live (paid) call is not permitted by the current mode/budget/price settings."""

    code = "llm_live_refused"


class BudgetExceeded(LiveCallRefused):
    code = "llm_budget_exceeded"


class ExternalLLMNotAllowed(LiveCallRefused):
    """Tenant data would leave the system but the tenant has not allowed external LLMs."""

    code = "llm_external_not_allowed"


# ----------------------------------------------------------------------------- request
@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    data: bytes
    media_type: str = "image/png"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def b64(self) -> str:
        return base64.standard_b64encode(self.data).decode("ascii")


Part = TextPart | ImagePart


@dataclass(frozen=True)
class LLMMessage:
    role: Role
    parts: tuple[Part, ...]

    @classmethod
    def user(cls, *parts: Part | str) -> LLMMessage:
        return cls("user", tuple(TextPart(p) if isinstance(p, str) else p for p in parts))


@dataclass(frozen=True)
class LLMRequest:
    """One structured-output call.

    ``tenant_id``: whose data is in the request (``"public"`` for public sources such as
    공정위 의결서 images). ``contains_tenant_data``: True when any part comes from a
    tenant's uploads/ledger -- then an external provider is used only with the tenant's
    ``allow_external_llm`` setting. ``prompt_id``/``prompt_version`` and ``tool_versions``
    are part of the replay key, so a prompt or tool change never reuses an old answer.
    """

    purpose: str
    system: str
    messages: tuple[LLMMessage, ...]
    schema: Mapping[str, Any]
    tenant_id: str
    contains_tenant_data: bool
    prompt_id: str
    prompt_version: str = "1"
    as_of: date | None = None
    tool_versions: Mapping[str, str] = field(default_factory=dict)
    max_output_tokens: int = 4096
    schema_name: str = "result"

    @property
    def prompt_hash(self) -> str:
        return content_hash(
            {
                "prompt_id": self.prompt_id,
                "prompt_version": self.prompt_version,
                "system": self.system,
                "schema": json.dumps(self.schema, sort_keys=True, ensure_ascii=False),
                "max_output_tokens": self.max_output_tokens,
            }
        )

    @property
    def input_hash(self) -> str:
        msgs = []
        for m in self.messages:
            parts: list[Any] = []
            for p in m.parts:
                if isinstance(p, TextPart):
                    parts.append({"text": p.text})
                else:
                    parts.append({"image_sha256": p.sha256, "media_type": p.media_type})
            msgs.append({"role": m.role, "parts": parts})
        return content_hash(msgs)

    @property
    def image_count(self) -> int:
        return sum(1 for m in self.messages for p in m.parts if isinstance(p, ImagePart))

    @property
    def text_bytes(self) -> int:
        n = len(self.system.encode("utf-8"))
        n += len(json.dumps(self.schema, ensure_ascii=False).encode("utf-8"))
        for m in self.messages:
            for p in m.parts:
                if isinstance(p, TextPart):
                    n += len(p.text.encode("utf-8"))
        return n

    def replay_key(self, model: str) -> str:
        return content_hash(
            {
                "tenant": self.tenant_id,
                "as_of": self.as_of,
                "input_hash": self.input_hash,
                "model": model,
                "prompt_hash": self.prompt_hash,
                "tool_versions": dict(sorted(self.tool_versions.items())),
            }
        )


# ----------------------------------------------------------------------------- response
@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    def to_json(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> TokenUsage:
        return cls(
            int(d.get("input_tokens", 0)),
            int(d.get("output_tokens", 0)),
            int(d.get("cache_read_tokens", 0)),
            int(d.get("cache_write_tokens", 0)),
        )


@dataclass(frozen=True)
class ProviderResponse:
    raw_text: str
    usage: TokenUsage
    model: str  # model that actually served the request
    stop_reason: str = ""
    request_id: str | None = None


@dataclass(frozen=True)
class LLMResult:
    data: Mapping[str, Any]
    raw_text: str
    usage: TokenUsage
    model: str
    provider: str
    run_id: str
    replay_key: str
    prompt_hash: str
    input_hash: str
    from_cache: bool
    cost_krw: Decimal | None  # None = unknown price (only with allow_unknown_price)
    attempts: int = 1
    latency_ms: int = 0


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str
    is_external: bool  # True: requests leave this system (commercial API)
    supports_vision: bool

    def complete(self, request: LLMRequest, *, timeout_s: float) -> ProviderResponse:
        """Send one request. Raise :class:`TransientLLMError` for retryable failures and
        :class:`LLMError` for the rest. Must not retry internally."""
        ...


# ----------------------------------------------------------------------------- helpers
def new_run_id(prefix: str = "llm") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse the model output as one JSON object (tolerates a ```json fence)."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        obj = json.loads(s, parse_float=Decimal)
    except json.JSONDecodeError as e:
        raise SchemaValidationError(f"output is not valid JSON: {e}", raw_text=text) from e
    if not isinstance(obj, dict):
        raise SchemaValidationError("output is not a JSON object", raw_text=text)
    return obj


def validate_schema(data: Any, schema: Mapping[str, Any], *, raw_text: str = "") -> None:
    """JSON-schema validation (Draft 2020-12). Raises :class:`SchemaValidationError`."""
    import jsonschema

    validator = jsonschema.Draft202012Validator(dict(schema))
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        msg = "; ".join(
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors[:5]
        )
        raise SchemaValidationError(f"output does not match schema: {msg}", raw_text=raw_text)


def call_with_retry(
    fn: Callable[[], ProviderResponse],
    *,
    max_retries: int,
    backoff_s: float,
    sleep: Callable[[float], None] = time.sleep,
    on_failed_attempt: Callable[[TransientLLMError], None] | None = None,
) -> tuple[ProviderResponse, int]:
    """Run ``fn`` with at most ``max_retries`` retries on :class:`TransientLLMError`
    (exponential backoff). Returns (response, attempts)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn(), attempt
        except TransientLLMError as e:
            if on_failed_attempt is not None:
                on_failed_attempt(e)
            if attempt > max_retries:
                raise
            sleep(backoff_s * (2 ** (attempt - 1)))


def strict_object(properties: Mapping[str, Any], *, required: Sequence[str] | None = None) -> dict:
    """Object schema accepted by provider-side structured output (all keys required, no
    additional properties; optional values are expressed as ``["...", "null"]``)."""
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required if required is not None else properties.keys()),
        "additionalProperties": False,
    }
