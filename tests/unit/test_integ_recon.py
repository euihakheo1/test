"""Integrator regressions for the matcher (hand-written fixtures only)."""

import random
from datetime import date, timedelta

from jettae.domain import Money, ReconcileStatus
from jettae.recon import Item, Payment, ReconConfig, check_conservation, normalize_ref, reconcile

D0 = date(2025, 1, 10)


def _item(iid, amt, group="", ref=""):
    return Item(iid, "ga", Money(amt), D0, normalize_ref(ref), normalize_ref(group))


def _pay(pid, amt, ref="", d=date(2025, 2, 10)):
    return Payment(pid, "ga", Money(amt), d, normalize_ref(ref), "")


def test_reference_subset_with_fee_tolerance_writes_off_shortfall():
    # 600,000 + 400,000 = 1,000,000 is the only subset in [999,500, 1,000,500]; the 500 won
    # shortfall must be written off as a fee, never allocated (used to raise ConservationError).
    items = [
        _item("L1", 600_000, group="S-001"),
        _item("L2", 400_000, group="S-001"),
        _item("L3", 300_000, group="S-001"),
    ]
    pays = [_pay("P1", 999_500, ref="S-001")]
    res = reconcile(items, pays, ReconConfig(fee_tolerance=Money(1000)))
    assert res.payments["P1"].allocated == Money(999_500)
    assert res.items["L1"].status is ReconcileStatus.MATCHED
    assert res.items["L2"].status is ReconcileStatus.MATCHED
    assert res.items["L3"].status is ReconcileStatus.UNMATCHED
    fees = res.items["L1"].fee_difference + res.items["L2"].fee_difference
    assert fees == Money(500)
    assert not check_conservation(
        res.allocations, {p.id: p.amount for p in pays}, {i.id: i.amount for i in items}
    )


def test_random_reference_scenarios_with_tolerance_never_crash():
    rng = random.Random(20261006)
    for _ in range(400):
        n = rng.randint(2, 6)
        items = [
            _item(f"L{k}", rng.choice([1, 2, 3, 4, 5, 6]) * 100_000, group="S-1") for k in range(n)
        ]
        target = sum(i.amount.amount for i in rng.sample(items, rng.randint(1, n)))
        pays = [
            _pay(
                "P1",
                target - rng.randint(0, 1500),
                ref="S-1",
                d=D0 + timedelta(days=rng.randint(0, 30)),
            )
        ]
        tol = Money(rng.choice([0, 500, 1000, 2000]))
        res = reconcile(items, pays, ReconConfig(fee_tolerance=tol))
        assert not check_conservation(
            res.allocations, {p.id: p.amount for p in pays}, {i.id: i.amount for i in items}
        )
