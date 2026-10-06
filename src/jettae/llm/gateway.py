"""The single entry point for LLM calls: mode, replay, tenant consent, budget, retry, schema.

Modes (``JETTAE_LLM_MODE``):
- ``offline`` (default) and ``replay``: no provider call. A request is answered only from the
  replay cache; a miss raises :class:`ReplayMiss`.
- ``live``: allowed only with ``JETTAE_LLM_BUDGET_KRW > 0`` and a known model price. A
  cached answer for the exact same key is reused (no cost); otherwise the maximum cost is
  reserved, the provider is called (limited retry on transient errors), the actual cost is
  settled and the answer is recorded for replay.

Tenant data (``request.contains_tenant_data``) is sent to an external provider only when the
tenant's ``allow_external_llm`` setting is true (:class:`TenantLLMPolicy`).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from jettae.llm.base import (
    ExternalLLMNotAllowed,
    LiveCallRefused,
    LLMError,
    LLMProvider,
    LLMRefusal,
    LLMRequest,
    LLMResult,
    ProviderResponse,
    ReplayMiss,
    SchemaValidationError,
    TokenUsage,
    TransientLLMError,
    call_with_retry,
    new_run_id,
    parse_json_object,
    validate_schema,
)
from jettae.llm.budget import (
    DEFAULT_RESERVATION_TTL_S,
    Budget,
    ModelPrice,
    PriceTable,
    max_cost_krw,
)
from jettae.llm.budget_store import SqlBudgetStore
from jettae.llm.replay import FileReplayStore, ReplayStore, make_entry


class LLMMode(StrEnum):
    OFFLINE = "offline"
    REPLAY = "replay"
    LIVE = "live"


# ----------------------------------------------------------------------------- tenant policy
class TenantLLMPolicy(Protocol):
    def allow_external_llm(self, tenant_id: str) -> bool: ...


@dataclass
class StaticTenantPolicy:
    allowed: frozenset[str] = frozenset()

    def allow_external_llm(self, tenant_id: str) -> bool:
        return tenant_id in self.allowed


class FileTenantPolicy:
    """``{"<tenant_id>": {"allow_external_llm": true}}``; anything else means False.

    Implements :class:`TenantLLMPolicy` from a JSON file (``JETTAE_TENANT_SETTINGS``). The
    tenant table has no ``allow_external_llm`` column, so a DB-backed policy would implement
    the same protocol. Missing file = no tenant allows external LLMs (deny by default)."""

    def __init__(self, path: Path | str | None) -> None:
        self.path = Path(path) if path else None

    def allow_external_llm(self, tenant_id: str) -> bool:
        if self.path is None or not self.path.exists():
            return False
        data = json.loads(self.path.read_text(encoding="utf-8"))
        entry = data.get(tenant_id)
        return isinstance(entry, Mapping) and entry.get("allow_external_llm") is True


# ----------------------------------------------------------------------------- config
@dataclass(frozen=True)
class GatewayConfig:
    mode: LLMMode = LLMMode.OFFLINE
    timeout_s: float = 120.0
    max_retries: int = 2  # transient errors (rate limit, overload, timeout, connection)
    backoff_s: float = 2.0
    schema_retries: int = 1  # one extra attempt when the output fails schema validation
    allow_unknown_price: bool = False


@dataclass(frozen=True)
class CallRecord:
    run_id: str
    purpose: str
    model: str
    usage: TokenUsage
    cost_krw: Decimal | None
    from_cache: bool
    replay_key: str
    attempts: int


@dataclass
class LLMGateway:
    provider: LLMProvider | None
    model: str
    provider_name: str
    store: ReplayStore
    config: GatewayConfig = field(default_factory=GatewayConfig)
    budget: Budget | None = None
    prices: PriceTable = field(default_factory=PriceTable)
    tenant_policy: TenantLLMPolicy = field(default_factory=StaticTenantPolicy)
    sleep: Callable[[float], None] = time.sleep
    records: list[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def for_provider(cls, provider: LLMProvider, store: ReplayStore, **kw: Any) -> LLMGateway:
        return cls(
            provider=provider, model=provider.model, provider_name=provider.name, store=store, **kw
        )

    # ------------------------------------------------------------------ public
    @property
    def live(self) -> bool:
        return self.config.mode is LLMMode.LIVE

    def complete(self, request: LLMRequest, *, run_id: str | None = None) -> LLMResult:
        rid = run_id or new_run_id()
        key = request.replay_key(self.model)
        cached = self.store.get(request.tenant_id, key)
        if cached is not None:
            return self._from_cache(request, cached, key, rid)
        if not self.live:
            raise ReplayMiss(
                f"no recorded LLM response for {request.purpose} (key {key[:12]}..., model "
                f"{self.model}); mode={self.config.mode.value}. Recording needs "
                "JETTAE_LLM_MODE=live and JETTAE_LLM_BUDGET_KRW>0."
            )
        return self._live(request, key, rid)

    def totals(self) -> dict[str, Any]:
        usage = TokenUsage()
        cost = Decimal(0)
        unknown = False
        for r in self.records:
            usage = usage + r.usage
            if r.cost_krw is None:
                unknown = True
            else:
                cost += r.cost_krw
        return {
            "calls": len(self.records),
            "cached_calls": sum(1 for r in self.records if r.from_cache),
            "usage": usage.to_json(),
            "cost_krw": str(cost),
            "cost_complete": not unknown,
        }

    # ------------------------------------------------------------------ internals
    def _record(self, rec: CallRecord) -> None:
        with self._lock:
            self.records.append(rec)

    def _from_cache(
        self, request: LLMRequest, entry: Mapping[str, Any], key: str, rid: str
    ) -> LLMResult:
        raw = str(entry["raw_text"])
        data = parse_json_object(raw)
        validate_schema(data, request.schema, raw_text=raw)
        usage = TokenUsage.from_json(entry.get("usage", {}))
        self._record(CallRecord(rid, request.purpose, self.model, usage, Decimal(0), True, key, 0))
        return LLMResult(
            data=data,
            raw_text=raw,
            usage=usage,
            model=str(entry.get("model", self.model)),
            provider=str(entry.get("provider", self.provider_name)),
            run_id=rid,
            replay_key=key,
            prompt_hash=request.prompt_hash,
            input_hash=request.input_hash,
            from_cache=True,
            cost_krw=Decimal(0),
            attempts=0,
        )

    def _check_live_allowed(self, request: LLMRequest) -> tuple[LLMProvider, ModelPrice | None]:
        if self.budget is None or self.budget.limit_krw <= 0:
            raise LiveCallRefused("live LLM calls need JETTAE_LLM_BUDGET_KRW > 0")
        if self.provider is None:
            raise LLMError("live mode but no LLM provider is configured")
        p = self.provider
        if (
            p.is_external
            and request.contains_tenant_data
            and not (self.tenant_policy.allow_external_llm(request.tenant_id))
        ):
            raise ExternalLLMNotAllowed(
                f"tenant {request.tenant_id!r} has not enabled allow_external_llm; "
                f"its data is not sent to {p.name}"
            )
        if request.image_count and not p.supports_vision:
            raise LLMError(f"provider {p.name} does not accept images")
        price = self.prices.get(p.model)
        if price is None and not self.config.allow_unknown_price:
            raise LiveCallRefused(
                f"price of model {p.model!r} is unknown (set JETTAE_LLM_PRICES or "
                "JETTAE_LLM_KRW_PER_USD); refusing the live call"
            )
        return p, price

    def _live(self, request: LLMRequest, key: str, rid: str) -> LLMResult:
        provider, price = self._check_live_allowed(request)
        assert self.budget is not None
        budget = self.budget
        total_usage = TokenUsage()
        total_cost: Decimal | None = Decimal(0)
        attempts = 0
        started = time.monotonic()
        last_error: SchemaValidationError | None = None
        for _ in range(self.config.schema_retries + 1):
            reservation_box: list[Any] = []

            def attempt(reservation_box: list[Any] = reservation_box) -> ProviderResponse:
                amount = max_cost_krw(request, price) if price is not None else Decimal(0)
                r = budget.reserve(amount, model=provider.model, purpose=request.purpose)
                reservation_box.append(r)
                try:
                    return provider.complete(request, timeout_s=self.config.timeout_s)
                # Billing policy for failed attempts: the reservation is an upper bound, so
                # a failure that may have reached the model (timeout / 5xx: maybe_billed, or
                # an unexpected exception or interrupt while the request was in flight) is
                # settled at the full reserved amount. Only a failure the provider reported
                # as not processed (rate limit, 4xx -> LLMError) is settled at 0.
                except TransientLLMError as e:
                    budget.settle(
                        r,
                        r.amount_krw if e.maybe_billed else Decimal(0),
                        note=f"failed attempt{' (possibly billed)' if e.maybe_billed else ''}: {e}",
                    )
                    raise
                except LLMError as e:
                    budget.settle(r, Decimal(0), note=f"rejected: {e}")
                    raise
                except BaseException as e:
                    budget.settle(
                        r, r.amount_krw, note=f"unexpected failure, possibly billed: {e!r}"
                    )
                    raise

            resp, n = call_with_retry(
                attempt,
                max_retries=self.config.max_retries,
                backoff_s=self.config.backoff_s,
                sleep=self.sleep,
            )
            attempts += n
            r = reservation_box[-1]
            cost = price.cost(resp.usage) if price is not None else None
            budget.settle(r, cost if cost is not None else Decimal(0), note=resp.stop_reason)
            total_usage = total_usage + resp.usage
            total_cost = None if (total_cost is None or cost is None) else total_cost + cost
            if resp.stop_reason == "refusal":
                self._record(
                    CallRecord(
                        rid,
                        request.purpose,
                        resp.model,
                        total_usage,
                        total_cost,
                        False,
                        key,
                        attempts,
                    )
                )
                raise LLMRefusal(f"{provider.name} declined the request")
            if resp.stop_reason == "max_tokens":
                self._record(
                    CallRecord(
                        rid,
                        request.purpose,
                        resp.model,
                        total_usage,
                        total_cost,
                        False,
                        key,
                        attempts,
                    )
                )
                raise LLMError("output truncated at max_output_tokens")
            try:
                data = parse_json_object(resp.raw_text)
                validate_schema(data, request.schema, raw_text=resp.raw_text)
            except SchemaValidationError as e:
                last_error = e
                continue
            self._record(
                CallRecord(
                    rid, request.purpose, resp.model, total_usage, total_cost, False, key, attempts
                )
            )
            self.store.put(
                request.tenant_id,
                key,
                make_entry(
                    request,
                    key=key,
                    provider=provider.name,
                    model=resp.model,
                    raw_text=resp.raw_text,
                    data=data,
                    usage=resp.usage,
                    run_id=rid,
                    cost_krw=str(cost) if cost is not None else None,
                ),
            )
            return LLMResult(
                data=data,
                raw_text=resp.raw_text,
                usage=total_usage,
                model=resp.model,
                provider=provider.name,
                run_id=rid,
                replay_key=key,
                prompt_hash=request.prompt_hash,
                input_hash=request.input_hash,
                from_cache=False,
                cost_krw=total_cost,
                attempts=attempts,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        self._record(
            CallRecord(
                rid, request.purpose, self.model, total_usage, total_cost, False, key, attempts
            )
        )
        assert last_error is not None
        raise last_error


# ----------------------------------------------------------------------------- from env
def _env_decimal(env: Mapping[str, str], name: str, default: str = "0") -> Decimal:
    raw = env.get(name, default).strip() or default
    return Decimal(raw)


LEDGER_NAME = "llm_budget.jsonl"


def default_ledger_path(env: Mapping[str, str]) -> Path:
    """Absolute per-user location of the live-mode budget ledger, independent of the
    working directory (two runs started from different directories must share it):

    ``JETTAE_STATE_DIR`` if set, else ``%LOCALAPPDATA%\\jettae`` on Windows, else
    ``$XDG_STATE_HOME/jettae``, else ``~/.local/state/jettae``; file ``llm_budget.jsonl``.
    """
    base = env.get("JETTAE_STATE_DIR")
    if not base:
        if env.get("LOCALAPPDATA"):
            base = str(Path(env["LOCALAPPDATA"]) / "jettae")
        elif env.get("XDG_STATE_HOME"):
            base = str(Path(env["XDG_STATE_HOME"]) / "jettae")
        else:
            home = env.get("HOME") or env.get("USERPROFILE") or str(Path.home())
            base = str(Path(home) / ".local" / "state" / "jettae")
    path = Path(base).expanduser()
    if not path.is_absolute():
        raise LLMError(
            f"JETTAE_STATE_DIR must be an absolute path (got {base!r}): a relative budget "
            "ledger would depend on the working directory"
        )
    return path / LEDGER_NAME


def budget_from_env(
    env: Mapping[str, str], *, limit: Decimal | None = None, live: bool = False
) -> Budget:
    """Budget with a shared store chosen from the environment.

    - ``JETTAE_LLM_BUDGET_DB`` (SQLAlchemy URL): :class:`SqlBudgetStore` in that database,
      budget row ``JETTAE_LLM_BUDGET_ID`` (default ``default``). For services / workers on
      several machines.
    - else ``JETTAE_LLM_LEDGER`` (absolute path): :class:`FileLockBudgetStore`.
    - else, in live mode, the per-user file ledger :func:`default_ledger_path` (absolute,
      independent of the working directory).
    - else (offline / replay: no paid call is possible) a process-local memory store.

    Sharing guarantee: every process that resolves to the same store shares one limit - a
    file ledger is shared by the processes of one user on one disk (an advisory file lock
    serialises reserve/settle), a SQL store by every process using that database. In live
    mode a relative ``JETTAE_LLM_LEDGER`` is refused (``LLMError``), because two runs from
    different directories would each get their own ledger and could each spend the full
    limit.

    ``JETTAE_LLM_BUDGET_KRW`` is a cumulative cap over everything recorded in that store
    (it is not reset per run; a fresh budget needs another ledger path or budget id).
    ``JETTAE_LLM_RESERVATION_TTL_S`` (default 900) is when an unsettled reservation is
    treated as belonging to a crashed process (it keeps counting, see budget_store)."""
    lim = _env_decimal(env, "JETTAE_LLM_BUDGET_KRW") if limit is None else limit
    ttl = float(env.get("JETTAE_LLM_RESERVATION_TTL_S") or DEFAULT_RESERVATION_TTL_S)
    db = env.get("JETTAE_LLM_BUDGET_DB")
    if db:
        store = SqlBudgetStore(db, budget_id=env.get("JETTAE_LLM_BUDGET_ID") or "default")
        return Budget(lim, store=store, reservation_ttl_s=ttl)
    configured = env.get("JETTAE_LLM_LEDGER")
    if configured:
        path = Path(configured).expanduser()
        if live and not path.is_absolute():
            raise LLMError(
                f"JETTAE_LLM_LEDGER must be an absolute path in live mode (got {configured!r})"
            )
        return Budget(lim, ledger_path=path, reservation_ttl_s=ttl)
    if live:
        return Budget(lim, ledger_path=default_ledger_path(env), reservation_ttl_s=ttl)
    return Budget(lim, ledger_path=None, reservation_ttl_s=ttl)


def gateway_from_env(
    env: Mapping[str, str] | None = None,
    *,
    mode: LLMMode | str | None = None,
    provider: LLMProvider | None = None,
) -> LLMGateway:
    """Build the gateway from ``JETTAE_LLM_*`` settings. The provider client is created only
    in live mode (offline/replay never constructs an SDK client)."""
    e: Mapping[str, str] = os.environ if env is None else env
    m = LLMMode(mode or e.get("JETTAE_LLM_MODE", "offline") or "offline")
    name = (e.get("JETTAE_LLM_PROVIDER") or "anthropic").lower()
    if name == "anthropic":
        from jettae.llm.anthropic import DEFAULT_MODEL

        model = e.get("JETTAE_LLM_MODEL") or DEFAULT_MODEL
    elif name == "openai":
        model = e.get("JETTAE_OPENAI_MODEL") or e.get("JETTAE_LLM_MODEL") or ""
        if not model:
            raise LLMError("set JETTAE_OPENAI_MODEL for JETTAE_LLM_PROVIDER=openai")
    else:
        raise LLMError(f"unknown JETTAE_LLM_PROVIDER {name!r} (anthropic | openai)")
    limit = _env_decimal(e, "JETTAE_LLM_BUDGET_KRW")
    if provider is None and m is LLMMode.LIVE and limit <= 0:
        # refuse before any budget store (file / database) is opened
        raise LiveCallRefused("JETTAE_LLM_MODE=live needs JETTAE_LLM_BUDGET_KRW > 0")
    budget = budget_from_env(e, limit=limit, live=m is LLMMode.LIVE)
    if provider is None and m is LLMMode.LIVE:
        try:
            if name == "anthropic":
                from jettae.llm.anthropic import AnthropicProvider

                provider = AnthropicProvider(model)
            else:
                from jettae.llm.openai import OpenAIProvider

                provider = OpenAIProvider(model)
        except LLMError:
            raise
        except Exception as exc:  # SDK client construction (missing credentials, ...)
            raise LLMError(f"cannot create the {name} client: {exc}") from exc
    return LLMGateway(
        provider=provider,
        model=provider.model if provider is not None else model,
        provider_name=provider.name if provider is not None else name,
        store=FileReplayStore(e.get("JETTAE_LLM_CACHE_DIR") or "var/llm_cache"),
        config=GatewayConfig(
            mode=m,
            timeout_s=float(e.get("JETTAE_LLM_TIMEOUT_S") or 120),
            max_retries=int(e.get("JETTAE_LLM_MAX_RETRIES") or 2),
            allow_unknown_price=(e.get("JETTAE_LLM_ALLOW_UNKNOWN_PRICE") == "1"),
        ),
        budget=budget,
        prices=PriceTable.from_env(e),
        tenant_policy=FileTenantPolicy(e.get("JETTAE_TENANT_SETTINGS")),
    )
