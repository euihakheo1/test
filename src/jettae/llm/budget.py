"""Paid-call budget (KRW, ``Decimal``): reserve the maximum cost before a call, settle after.

Rules (AGENTS.md rule 5, SPEC §2):
- default mode is offline: no provider call at all (replay cache only);
- a live call needs ``JETTAE_LLM_MODE=live`` AND ``JETTAE_LLM_BUDGET_KRW > 0``;
- the price of the model must be known (KRW per million tokens), otherwise the call is
  refused unless ``allow_unknown_price`` is set explicitly;
- before each call the *maximum* possible cost is reserved (conservative token estimate for
  the input, ``max_output_tokens`` for the output); a call whose reservation does not fit in
  the remaining budget is refused before anything is sent;
- the remaining budget is shared: reservations and settlements go through one atomic
  :mod:`jettae.llm.budget_store` (memory, file-lock ledger or service DB), so concurrent
  ``Budget`` objects and processes cannot together exceed the limit.

Prices: a small built-in table of Anthropic list prices in USD per million tokens (from the
Anthropic model table, cached 2026-09-25) is converted with ``JETTAE_LLM_KRW_PER_USD``; no
exchange rate is assumed. ``JETTAE_LLM_PRICES`` (JSON) can set KRW prices directly:
``{"model-id": {"input_krw_per_mtok": "6000", "output_krw_per_mtok": "30000"}}``.
OpenAI prices are not built in (not verified here) -- set them with ``JETTAE_LLM_PRICES``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from jettae.llm.base import BudgetExceeded, LiveCallRefused, LLMError, LLMRequest, TokenUsage
from jettae.llm.budget_store import (
    BudgetStore,
    BudgetTotals,
    FileLockBudgetStore,
    MemoryBudgetStore,
    Reservation,
    q4,
)

__all__ = [
    "Budget",
    "BudgetStore",
    "BudgetTotals",
    "ModelPrice",
    "PriceTable",
    "Reservation",
    "estimate_max_input_tokens",
    "max_cost_krw",
]

MTOK = Decimal(1_000_000)

# USD per million tokens (input, output). Source: Anthropic model table in the claude-api
# reference, "cached: 2026-09-25". Cache write = 1.25x input, cache read = 0.1x input.
ANTHROPIC_USD_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-fable-5-1": (Decimal("10"), Decimal("50")),
    "claude-fable-5": (Decimal("10"), Decimal("50")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-opus-4-8": (Decimal("5"), Decimal("25")),
    "claude-opus-4-7": (Decimal("5"), Decimal("25")),
    "claude-opus-4-6": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-sonnet-4-6": (Decimal("3"), Decimal("15")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}

# Conservative per-image token allowance used only for the reservation (images are
# downscaled by the providers; a 1.15 MP image is ~1,600 tokens on Claude).
DEFAULT_IMAGE_TOKENS = 6000
MESSAGE_OVERHEAD_TOKENS = 64


@dataclass(frozen=True)
class ModelPrice:
    input_krw_per_mtok: Decimal
    output_krw_per_mtok: Decimal
    source: str

    def cost(self, usage: TokenUsage) -> Decimal:
        inp = (
            Decimal(usage.input_tokens)
            + Decimal(usage.cache_write_tokens) * Decimal("1.25")
            + Decimal(usage.cache_read_tokens) * Decimal("0.1")
        )
        return (
            inp * self.input_krw_per_mtok + Decimal(usage.output_tokens) * self.output_krw_per_mtok
        ) / MTOK


class PriceTable:
    def __init__(
        self,
        prices: Mapping[str, ModelPrice] | None = None,
        *,
        krw_per_usd: Decimal | None = None,
        usd_table: Mapping[str, tuple[Decimal, Decimal]] = ANTHROPIC_USD_PER_MTOK,
    ) -> None:
        self._prices = dict(prices or {})
        self.krw_per_usd = krw_per_usd
        self._usd = dict(usd_table)

    def get(self, model: str) -> ModelPrice | None:
        if model in self._prices:
            return self._prices[model]
        if model in self._usd and self.krw_per_usd is not None:
            i, o = self._usd[model]
            return ModelPrice(
                i * self.krw_per_usd,
                o * self.krw_per_usd,
                f"builtin USD list price x {self.krw_per_usd} KRW/USD",
            )
        return None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PriceTable:
        """Prices from ``JETTAE_LLM_PRICES`` / ``JETTAE_LLM_KRW_PER_USD``. A value that is not
        a positive number (e.g. an unedited ``change-me`` placeholder from an example file)
        is a configuration error (:class:`LLMError`), never a silently unknown price."""
        e = os.environ if env is None else env
        fx = (e.get("JETTAE_LLM_KRW_PER_USD") or "").strip()
        prices: dict[str, ModelPrice] = {}
        raw = (e.get("JETTAE_LLM_PRICES") or "").strip()
        try:
            if raw:
                for model, p in json.loads(raw).items():
                    prices[model] = ModelPrice(
                        _positive(p["input_krw_per_mtok"]),
                        _positive(p["output_krw_per_mtok"]),
                        "JETTAE_LLM_PRICES",
                    )
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise LLMError(
                'JETTAE_LLM_PRICES must be JSON like {"<model>": {"input_krw_per_mtok": '
                '"<KRW>", "output_krw_per_mtok": "<KRW>"}} with positive numbers'
            ) from exc
        try:
            rate = _positive(fx) if fx else None
        except ValueError:
            raise LLMError("JETTAE_LLM_KRW_PER_USD must be a positive number") from None
        return cls(prices, krw_per_usd=rate)


def _positive(value: Any) -> Decimal:
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("not a number") from None
    if not d.is_finite() or d <= 0:
        raise ValueError("not a positive number")
    return d


def estimate_max_input_tokens(request: LLMRequest, image_tokens: int = DEFAULT_IMAGE_TOKENS) -> int:
    """Upper bound for the reservation: one token per UTF-8 byte of text (a token always
    covers at least one byte), a fixed allowance per image, a per-message overhead."""
    return (
        request.text_bytes
        + request.image_count * image_tokens
        + MESSAGE_OVERHEAD_TOKENS * (len(request.messages) + 1)
    )


DEFAULT_RESERVATION_TTL_S = 900.0


class Budget:
    """KRW budget whose state lives in a shared :class:`~jettae.llm.budget_store.BudgetStore`.

    Concurrency guarantee: ``reserve`` is one atomic check-and-insert in the store, so any
    number of ``Budget`` objects (threads, processes, hosts for the SQL store) that use the
    same store never reserve more than ``limit_krw`` together -- in-flight reservations of
    the others are counted, not only settled costs. Every property reads the store, so an
    object never works from a total it loaded earlier.

    Store selection: ``store`` if given; else ``ledger_path`` -> :class:`FileLockBudgetStore`
    (cross-process on one machine); else a process-local :class:`MemoryBudgetStore`.

    Reservations expire after ``reservation_ttl_s`` but keep counting until reconciled (see
    :mod:`jettae.llm.budget_store` for the crashed-process / possibly-billed policy).
    """

    def __init__(
        self,
        limit_krw: Decimal,
        ledger_path: Path | None = None,
        *,
        store: BudgetStore | None = None,
        reservation_ttl_s: float = DEFAULT_RESERVATION_TTL_S,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.limit_krw = Decimal(limit_krw)
        self.ledger_path = Path(ledger_path) if ledger_path is not None else None
        if store is None:
            store = (
                FileLockBudgetStore(self.ledger_path)
                if self.ledger_path is not None
                else MemoryBudgetStore()
            )
        self.store: BudgetStore = store
        self.reservation_ttl_s = reservation_ttl_s
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        return self._clock()

    def totals(self) -> BudgetTotals:
        return self.store.totals(now=self._now())

    @property
    def spent_krw(self) -> Decimal:
        """Settled costs plus reconciled (possibly billed) reservations, all spenders."""
        return self.totals().spent_krw

    @property
    def reserved_krw(self) -> Decimal:
        """Open reservations of all spenders (expired ones included until reconciled)."""
        return self.totals().reserved_krw

    @property
    def remaining_krw(self) -> Decimal:
        return self.limit_krw - self.totals().committed_krw

    def reserve(self, amount_krw: Decimal, *, model: str, purpose: str) -> Reservation:
        if self.limit_krw <= 0:
            raise LiveCallRefused("LLM budget is 0 KRW: live calls are disabled")
        now = self._now()
        r = Reservation(
            id=f"rsv_{uuid.uuid4().hex[:16]}",
            amount_krw=q4(amount_krw),
            model=model,
            purpose=purpose,
            created_at=now,
            expires_at=now + timedelta(seconds=self.reservation_ttl_s),
        )
        remaining = self.store.try_reserve(r, limit_krw=self.limit_krw)
        if remaining is not None:
            raise BudgetExceeded(
                f"reservation {r.amount_krw} KRW for {model} exceeds remaining budget "
                f"{remaining} KRW (settled + in-flight reservations of every spender)"
            )
        return r

    def settle(self, r: Reservation, actual_krw: Decimal, *, note: str = "") -> Decimal:
        """Release the reservation and record the actual cost (rounded up to 0.0001 KRW).
        Returns the recorded cost. ``RuntimeError`` when already settled (by any object)."""
        if r.settled:
            raise RuntimeError(f"reservation {r.id} already settled")
        recorded = q4(actual_krw)
        self.store.settle(r.id, recorded, note=note, now=self._now())
        r.settled = True
        return recorded

    def reconcile_expired(self, *, note: str = "expired: possibly billed") -> list[str]:
        """Count reservations past their TTL as billed at the reserved amount. Operator /
        maintenance action; a late ``settle`` by the original process still replaces it."""
        return self.store.reconcile_expired(now=self._now(), note=note)

    def snapshot(self) -> dict[str, Any]:
        t = self.totals()
        return {
            "limit_krw": str(self.limit_krw),
            "spent_krw": str(t.spent_krw),
            "reserved_krw": str(t.reserved_krw),
            "expired_open_krw": str(t.expired_open_krw),
            "possibly_billed_krw": str(t.possibly_billed_krw),
            "remaining_krw": str(self.limit_krw - t.committed_krw),
        }


def max_cost_krw(request: LLMRequest, price: ModelPrice) -> Decimal:
    est_in = estimate_max_input_tokens(request)
    # charge every input token at the cache-write rate (the most expensive input class)
    worst = TokenUsage(input_tokens=0, output_tokens=request.max_output_tokens)
    return Decimal(est_in) * Decimal("1.25") * price.input_krw_per_mtok / MTOK + price.cost(worst)
