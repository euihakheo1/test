"""Allocation book with conservation guarantees.

Invariants (checked on every commit and by :func:`check_conservation`):
- each allocation has the sign of its target item and is non-zero;
- per item: |allocated + fee write-off| <= |item amount| (no double use of an item);
- per payment: allocations sum has the payment's sign and |sum| <= |payment amount|
  (credits netted inside one payment may make individual lines negative).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from jettae.domain.errors import ConservationError
from jettae.domain.models import Allocation
from jettae.domain.money import Money


def _sign(x: int) -> int:
    return (x > 0) - (x < 0)


@dataclass(frozen=True)
class Leg:
    item_id: str
    amount: Money
    fee: Money  # write-off accepted as payer-side fee/tolerance


class AllocationBook:
    def __init__(self, payments: Mapping[str, Money], items: Mapping[str, Money]) -> None:
        self._pay_amt = dict(payments)
        self._item_amt = dict(items)
        self._pay_used = {k: Money.zero(v.currency) for k, v in payments.items()}
        self._item_alloc = {k: Money.zero(v.currency) for k, v in items.items()}
        self._item_fee = {k: Money.zero(v.currency) for k, v in items.items()}
        self._allocs: dict[tuple[str, str], Allocation] = {}

    # -- queries --------------------------------------------------------
    def item_open(self, item_id: str) -> Money:
        return self._item_amt[item_id] - self._item_alloc[item_id] - self._item_fee[item_id]

    def item_allocated(self, item_id: str) -> Money:
        return self._item_alloc[item_id]

    def item_fee(self, item_id: str) -> Money:
        return self._item_fee[item_id]

    def payment_remaining(self, payment_id: str) -> Money:
        return self._pay_amt[payment_id] - self._pay_used[payment_id]

    def payment_used(self, payment_id: str) -> Money:
        return self._pay_used[payment_id]

    def is_untouched(self, payment_id: str) -> bool:
        return self._pay_used[payment_id].is_zero

    @property
    def allocations(self) -> tuple[Allocation, ...]:
        return tuple(self._allocs.values())

    # -- mutation -------------------------------------------------------
    def commit(self, payment_id: str, legs: Iterable[Leg], evidence: tuple[str, ...]) -> None:
        """Apply all legs atomically or raise ConservationError without changes."""
        legs = list(legs)
        pay_amt = self._pay_amt[payment_id]
        total = self._pay_used[payment_id]
        new_alloc: dict[str, Money] = {}
        new_fee: dict[str, Money] = {}
        for leg in legs:
            iid = leg.item_id
            item_amt = self._item_amt[iid]
            if leg.amount.is_zero and leg.fee.is_zero:
                raise ConservationError(f"empty leg for {iid}")
            for part in (leg.amount, leg.fee):
                if not part.is_zero and _sign(part.amount) != _sign(item_amt.amount):
                    raise ConservationError(f"allocation sign differs from item {iid}")
            a = new_alloc.get(iid, self._item_alloc[iid]) + leg.amount
            f = new_fee.get(iid, self._item_fee[iid]) + leg.fee
            if abs((a + f).amount) > abs(item_amt.amount):
                raise ConservationError(f"item {iid} over-allocated")
            new_alloc[iid], new_fee[iid] = a, f
            total = total + leg.amount
        if total.amount != 0 and _sign(total.amount) != _sign(pay_amt.amount):
            raise ConservationError(f"payment {payment_id} allocation sign differs")
        if abs(total.amount) > abs(pay_amt.amount):
            raise ConservationError(f"payment {payment_id} over-used")
        self._pay_used[payment_id] = total
        self._item_alloc.update(new_alloc)
        self._item_fee.update(new_fee)
        for leg in legs:
            if leg.amount.is_zero:
                continue
            key = (payment_id, leg.item_id)
            prev = self._allocs.get(key)
            if prev is None:
                self._allocs[key] = Allocation(payment_id, leg.item_id, leg.amount, evidence)
            else:  # same payment applied twice to one item: merge into one allocation
                ev = tuple(dict.fromkeys(prev.evidence + evidence))
                self._allocs[key] = Allocation(
                    payment_id, leg.item_id, prev.amount + leg.amount, ev
                )


def check_conservation(
    allocations: Iterable[Allocation],
    payments: Mapping[str, Money],
    items: Mapping[str, Money],
) -> list[str]:
    """Return a list of violations (empty = conserved)."""
    violations: list[str] = []
    per_pay: dict[str, int] = {}
    per_item: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for a in allocations:
        if a.source_id not in payments:
            violations.append(f"allocation from unknown payment {a.source_id}")
            continue
        if a.target_id not in items:
            violations.append(f"allocation to unknown item {a.target_id}")
            continue
        if a.amount.is_zero:
            violations.append(f"zero allocation {a.source_id}->{a.target_id}")
        if _sign(a.amount.amount) != _sign(items[a.target_id].amount):
            violations.append(f"sign mismatch {a.source_id}->{a.target_id}")
        if (a.source_id, a.target_id) in seen:
            violations.append(f"duplicate allocation {a.source_id}->{a.target_id}")
        seen.add((a.source_id, a.target_id))
        per_pay[a.source_id] = per_pay.get(a.source_id, 0) + a.amount.amount
        per_item[a.target_id] = per_item.get(a.target_id, 0) + a.amount.amount
    for pid, tot in per_pay.items():
        amt = payments[pid].amount
        if abs(tot) > abs(amt) or (tot != 0 and _sign(tot) != _sign(amt)):
            violations.append(f"payment {pid}: allocated {tot} exceeds payment {amt}")
    for iid, tot in per_item.items():
        if abs(tot) > abs(items[iid].amount):
            violations.append(f"item {iid}: allocated {tot} exceeds item {items[iid].amount}")
    return violations
