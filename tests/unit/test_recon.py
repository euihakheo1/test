import random
from datetime import date, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from jettae.domain import ConservationError, Money, ReconcileStatus
from jettae.recon import (
    AllocationBook,
    Item,
    Leg,
    Payment,
    ReconConfig,
    check_conservation,
    find_subsets,
    normalize_counterparty,
    normalize_ref,
    reconcile,
)

M = ReconcileStatus.MATCHED
D0 = date(2025, 1, 10)


def it(iid, amt, d=D0, ref="", group="", cp="ga"):
    return Item(iid, cp, Money(amt), d, normalize_ref(ref), normalize_ref(group))


def pay(pid, amt, d=date(2025, 2, 10), ref="", memo="", cp="ga"):
    return Payment(pid, cp, Money(amt), d, normalize_ref(ref), normalize_ref(memo))


def conserved(res, items, pays):
    return check_conservation(
        res.allocations, {p.id: p.amount for p in pays}, {i.id: i.amount for i in items}
    )


def test_normalisation():
    assert normalize_counterparty("(주)가나 유통") == normalize_counterparty("가나유통 주식회사")
    assert normalize_ref("inv-0001") == "INV0001"
    assert normalize_ref("0001") != normalize_ref("1")  # leading zeros preserved


def test_exact_reference_match_via_memo():
    items = [it("A", 1_000_000, ref="INV-0001"), it("B", 1_000_000, ref="INV-0002")]
    pays = [pay("P1", 1_000_000, memo="INV-0002 대금")]
    res = reconcile(items, pays)
    assert res.items["B"].status is M
    assert res.items["A"].status is ReconcileStatus.UNMATCHED
    assert res.payments["P1"].status is M
    assert res.allocations[0].evidence[0] == "reference"


def test_partial_then_rest():
    items = [it("A", 1_000_000, ref="INV-9")]
    pays = [pay("P1", 400_000, ref="INV-9"), pay("P2", 600_000, d=date(2025, 3, 1))]
    res = reconcile(items, pays)
    assert res.items["A"].status is M
    assert [a.amount for a in res.items["A"].allocations] == [Money(400_000), Money(600_000)]
    only_first = reconcile(items, pays[:1])
    assert only_first.items["A"].status is ReconcileStatus.PARTIAL
    assert only_first.items["A"].open == Money(600_000)


def test_partial_by_amount_single_candidate():
    res = reconcile([it("A", 1_000_000)], [pay("P1", 300_000)])
    assert res.items["A"].status is ReconcileStatus.PARTIAL
    assert res.items["A"].allocated == Money(300_000)


def test_one_payment_covers_several_invoices():
    items = [it("A", 1_000_000), it("B", 2_000_000), it("C", 3_500_000)]
    pays = [pay("P1", 3_000_000)]
    res = reconcile(items, pays)
    assert res.items["A"].status is M and res.items["B"].status is M
    assert res.items["C"].status is ReconcileStatus.UNMATCHED
    assert res.payments["P1"].status is M
    assert not conserved(res, items, pays)


def test_settlement_netting_with_deduction_lines():
    items = [
        it("L1", 1_000_000, group="S-2025-01"),
        it("L2", 500_000, group="S-2025-01"),
        it("L3", -100_000, group="S-2025-01"),  # 공제
    ]
    pays = [pay("P1", 1_400_000, memo="정산 S-2025-01")]
    res = reconcile(items, pays)
    assert all(res.items[i].status is M for i in ("L1", "L2", "L3"))
    assert res.items["L3"].allocated == Money(-100_000)
    assert not conserved(res, items, pays)
    # same without a reference: subset-sum with the credit line
    res2 = reconcile(items, [pay("P1", 1_400_000)])
    assert all(res2.items[i].status is M for i in ("L1", "L2", "L3"))


def test_refund_of_credit_and_reversal():
    items = [it("R1", -200_000), it("A", 500_000)]
    pays = [
        pay("P-REF", -200_000, d=date(2025, 2, 1)),  # supplier paid back a return
        pay("P1", 500_000, d=date(2025, 2, 3), cp="other"),
        pay("P1-REV", -500_000, d=date(2025, 2, 4), cp="other"),
    ]
    res = reconcile(items, pays)
    assert res.items["R1"].status is M
    assert res.payments["P-REF"].status is M
    assert res.payments["P1"].status is M and res.payments["P1-REV"].status is M
    assert any("취소" in r for r in res.payments["P1-REV"].reasons)
    assert res.items["A"].status is ReconcileStatus.UNMATCHED


def test_reversal_inside_group_leaves_invoice_open():
    items = [it("A", 500_000)]
    pays = [pay("P1", 500_000, d=date(2025, 2, 3)), pay("P2", -500_000, d=date(2025, 2, 4))]
    res = reconcile(items, pays)
    assert res.items["A"].status is ReconcileStatus.UNMATCHED
    assert res.allocations == ()


def test_equal_amounts_are_ambiguous():
    items = [it("A", 1_000_000, d=date(2025, 1, 10)), it("B", 1_000_000, d=date(2025, 1, 20))]
    pays = [pay("P1", 1_000_000)]
    res = reconcile(items, pays)
    assert res.items["A"].status is ReconcileStatus.AMBIGUOUS
    assert res.items["B"].status is ReconcileStatus.AMBIGUOUS
    assert res.payments["P1"].status is ReconcileStatus.AMBIGUOUS
    assert res.allocations == ()
    assert res.items["A"].candidates == ("P1",)


def test_two_equal_payments_two_equal_invoices_ambiguous():
    items = [it("A", 700), it("B", 700)]
    pays = [pay("P1", 700), pay("P2", 700, d=date(2025, 2, 11))]
    res = reconcile(items, pays)
    assert {r.status for r in res.items.values()} == {ReconcileStatus.AMBIGUOUS}


def test_fee_tolerance():
    cfg = ReconConfig(fee_tolerance=Money(1_000))
    res = reconcile([it("A", 1_000_000)], [pay("P1", 999_500)], cfg)
    assert res.items["A"].status is M
    assert res.items["A"].fee_difference == Money(500)
    assert res.items["A"].allocated == Money(999_500)
    strict = reconcile([it("A", 1_000_000)], [pay("P1", 999_500)])
    assert strict.items["A"].status is ReconcileStatus.PARTIAL


def test_date_window_excludes_far_payments():
    cfg = ReconConfig(days_after=30)
    res = reconcile([it("A", 1000, d=date(2025, 1, 1))], [pay("P1", 1000, d=date(2025, 6, 1))], cfg)
    assert res.items["A"].status is ReconcileStatus.UNMATCHED
    assert res.payments["P1"].status is ReconcileStatus.UNMATCHED


def test_reference_counterparty_conflict():
    items = [it("A", 1000, ref="INV-77", cp="ga")]
    res = reconcile(items, [pay("P1", 1000, ref="INV-77", cp="other")])
    assert res.items["A"].status is ReconcileStatus.CONFLICT


def test_candidate_and_node_caps_give_ambiguous():
    items = [it(f"I{k:02d}", 1000 + k) for k in range(30)]
    res = reconcile(items, [pay("P1", 5_000_000)], ReconConfig(max_candidates=10))
    assert res.payments["P1"].status is ReconcileStatus.AMBIGUOUS
    assert any("상한" in r for r in res.payments["P1"].reasons)
    items2 = [it(f"I{k:02d}", 100 + k) for k in range(12)]
    # 651 = sum of the six largest items: reachable, but deep in the search order
    res2 = reconcile(items2, [pay("P1", 651)], ReconConfig(max_nodes=50))
    assert res2.payments["P1"].status is ReconcileStatus.AMBIGUOUS
    assert any("탐색 상한" in r for r in res2.payments["P1"].reasons)


def test_find_subsets_bounds():
    s = find_subsets([5, 3, 2, -1], 4, 4, max_size=3)
    assert sorted(s.solutions) == [(0, 3), (1, 2, 3)][: len(s.solutions)]
    assert not s.exhausted
    s2 = find_subsets(list(range(1, 40)), 100, 100, max_nodes=10)
    assert s2.exhausted


def test_result_independent_of_input_order():
    items = [it("A", 300), it("B", 500), it("C", 200, d=date(2025, 1, 12)), it("D", 900)]
    pays = [pay("P1", 800), pay("P2", 900, d=date(2025, 2, 12)), pay("P3", 100)]
    base = reconcile(items, pays)
    rng = random.Random(7)
    for _ in range(5):
        i2, p2 = items[:], pays[:]
        rng.shuffle(i2)
        rng.shuffle(p2)
        assert reconcile(i2, p2) == base


def test_allocation_book_rejects_double_use():
    book = AllocationBook({"P1": Money(100), "P2": Money(100)}, {"A": Money(100)})
    book.commit("P1", [Leg("A", Money(100), Money(0))], ("t",))
    with pytest.raises(ConservationError):
        book.commit("P2", [Leg("A", Money(1), Money(0))], ("t",))
    with pytest.raises(ConservationError):
        AllocationBook({"P": Money(50)}, {"A": Money(100)}).commit(
            "P", [Leg("A", Money(60), Money(0))], ()
        )
    from jettae.domain import Allocation

    bad = [Allocation("P1", "A", Money(80)), Allocation("P2", "A", Money(80))]
    v = check_conservation(bad, {"P1": Money(100), "P2": Money(100)}, {"A": Money(100)})
    assert any("exceeds item" in x for x in v)


amounts = st.sampled_from([100, 200, 300, 500, 700, 1000, -100])


@settings(max_examples=150, deadline=None)
@given(
    st.lists(amounts, min_size=1, max_size=8),
    st.lists(
        st.sampled_from([100, 200, 300, 400, 500, 800, 1000, 1200, -100, -300]),
        min_size=1,
        max_size=6,
    ),
    st.integers(0, 2),
)
def test_conservation_property(item_amts, pay_amts, tol):
    items = [it(f"I{k}", a, d=D0 + timedelta(days=k)) for k, a in enumerate(item_amts)]
    pays = [pay(f"P{k}", a, d=date(2025, 2, 1) + timedelta(days=k)) for k, a in enumerate(pay_amts)]
    res = reconcile(items, pays, ReconConfig(fee_tolerance=Money(tol * 50)))
    assert conserved(res, items, pays) == []
    for r in res.items.values():
        assert abs((r.allocated + r.fee_difference).amount) <= abs(r.amount.amount)
        if r.status is M:
            assert r.open.is_zero
