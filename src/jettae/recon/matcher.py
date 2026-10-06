"""Deterministic reconciliation of receivable items against payments.

Passes (inputs sorted by (date, id); results do not depend on input order):

0. reversals: an outgoing payment that mirrors an earlier incoming one (same counterparty,
   same absolute amount) cancels it;
1. reference: payment reference / memo contains an item reference or settlement group ref
   (settlement groups net sales and deduction lines); partial and over-payment handled;
2. one-to-one amount match (+ optional fee tolerance) inside the date window; a match is
   taken only if it is unique in both directions, otherwise all parties are AMBIGUOUS;
3. bounded subset-sum: one payment covering several items (credits allowed) with caps on
   candidates / nodes / time; several solutions or an exhausted budget -> AMBIGUOUS;
4. partial: a payment smaller than exactly one open item is applied to it.
Passes 2-4 repeat until nothing changes.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from jettae.domain.models import Allocation
from jettae.domain.money import Money, sum_money
from jettae.domain.status import ReconcileStatus
from jettae.recon.allocation import AllocationBook, Leg
from jettae.recon.candidates import Item, Payment, ReconConfig, in_window, reference_hits

REVERSAL_DAYS = 31


@dataclass(frozen=True)
class ItemResult:
    item_id: str
    status: ReconcileStatus
    amount: Money
    allocated: Money
    fee_difference: Money
    open: Money
    allocations: tuple[Allocation, ...]
    candidates: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PaymentResult:
    payment_id: str
    status: ReconcileStatus
    amount: Money
    allocated: Money
    unapplied: Money
    candidates: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconResult:
    items: Mapping[str, ItemResult]
    payments: Mapping[str, PaymentResult]
    allocations: tuple[Allocation, ...]
    stats: Mapping[str, int] = field(default_factory=dict)


@dataclass
class SubsetSearch:
    solutions: list[tuple[int, ...]]
    exhausted: bool
    nodes: int


def find_subsets(
    values: list[int],
    lo: int,
    hi: int,
    *,
    min_size: int = 2,
    max_size: int = 6,
    max_nodes: int = 200_000,
    time_limit_s: float = 5.0,
    max_solutions: int = 2,
) -> SubsetSearch:
    """Index subsets (size in [min_size, max_size]) whose sum lies in [lo, hi].

    Stops after ``max_solutions`` solutions. ``exhausted`` is True when the node or time
    budget ran out before the search space was covered.
    """
    n = len(values)
    suf_pos = [0] * (n + 1)
    suf_neg = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suf_pos[i] = suf_pos[i + 1] + max(values[i], 0)
        suf_neg[i] = suf_neg[i + 1] + min(values[i], 0)
    sols: list[tuple[int, ...]] = []
    nodes = 0
    exhausted = False
    deadline = time.monotonic() + time_limit_s
    chosen: list[int] = []

    def dfs(i: int, s: int) -> bool:  # returns False to stop the whole search
        nonlocal nodes, exhausted
        nodes += 1
        if nodes > max_nodes or (nodes % 1024 == 0 and time.monotonic() > deadline):
            exhausted = True
            return False
        k = len(chosen)
        if k >= min_size and lo <= s <= hi:
            sols.append(tuple(chosen))
            if len(sols) >= max_solutions:
                return False
        if k == max_size or i == n:
            return True
        if s + suf_neg[i] > hi or s + suf_pos[i] < lo:
            return True
        for j in range(i, n):
            chosen.append(j)
            cont = dfs(j + 1, s + values[j])
            chosen.pop()
            if not cont:
                return False
        return True

    dfs(0, 0)
    return SubsetSearch(sols, exhausted, nodes)


def _sign(x: int) -> int:
    return (x > 0) - (x < 0)


class _Run:
    def __init__(self, items: Iterable[Item], payments: Iterable[Payment], cfg: ReconConfig):
        self.cfg = cfg
        self.items = sorted(items, key=lambda i: (i.date is None, i.date or 0, i.id))
        self.payments = sorted(payments, key=lambda p: (p.date, p.id))
        self.item_by_id = {i.id: i for i in self.items}
        self.book = AllocationBook(
            {p.id: p.amount for p in self.payments}, {i.id: i.amount for i in self.items}
        )
        self.i_flag: dict[str, set[str]] = {i.id: set() for i in self.items}
        self.i_reason: dict[str, list[str]] = {i.id: [] for i in self.items}
        self.i_cands: dict[str, set[str]] = {i.id: set() for i in self.items}
        self.p_flag: dict[str, set[str]] = {p.id: set() for p in self.payments}
        self.p_reason: dict[str, list[str]] = {p.id: [] for p in self.payments}
        self.p_cands: dict[str, set[str]] = {p.id: set() for p in self.payments}
        self.reversed: set[str] = set()
        self.stats = {"subset_nodes": 0, "subset_searches": 0}

    # -- helpers ----------------------------------------------------------
    def _open(self, iid: str) -> Money:
        return self.book.item_open(iid)

    def _item_free(self, iid: str) -> bool:
        return not self.i_flag[iid] and not self._open(iid).is_zero

    def _pay_free(self, p: Payment) -> bool:
        return (
            p.id not in self.reversed
            and not self.p_flag[p.id]
            and not self.book.payment_remaining(p.id).is_zero
        )

    def _flag_ambiguous(self, p: Payment, item_ids: Iterable[str], reason: str) -> None:
        ids = sorted(set(item_ids))
        self.p_flag[p.id].add("ambiguous")
        self.p_reason[p.id].append(reason)
        self.p_cands[p.id].update(ids)
        for iid in ids:
            self.i_flag[iid].add("ambiguous")
            self.i_cands[iid].add(p.id)
            if reason not in self.i_reason[iid]:
                self.i_reason[iid].append(reason)

    def _commit(self, p: Payment, legs: list[Leg], evidence: tuple[str, ...], reason: str) -> None:
        self.book.commit(p.id, legs, evidence)
        self.p_reason[p.id].append(reason)
        for leg in legs:
            self.i_reason[leg.item_id].append(f"{reason} ({p.id})")
            if not leg.fee.is_zero:
                self.i_reason[leg.item_id].append(
                    f"입금액과 차이 {leg.fee} (허용 오차 이내, 수수료 추정)"
                )

    def _full_legs(self, ids: list[str], shortfall: Money) -> list[Leg] | None:
        """Full legs for ``ids``; ``shortfall`` (>=0) is written off on the last positive
        item able to absorb it."""
        legs = [Leg(iid, self._open(iid), Money.zero(self._open(iid).currency)) for iid in ids]
        if shortfall.is_zero:
            return legs
        for idx in range(len(legs) - 1, -1, -1):
            o = legs[idx].amount
            if o.is_positive and o >= shortfall:
                legs[idx] = Leg(legs[idx].item_id, o - shortfall, shortfall)
                return legs
        return None

    def _same_cp(self, it: Item, p: Payment) -> bool:
        return not p.counterparty or not it.counterparty or it.counterparty == p.counterparty

    # -- pass 0: reversals ----------------------------------------------------
    def pass_reversals(self) -> None:
        for neg in self.payments:
            if not neg.amount.is_negative or neg.id in self.reversed:
                continue
            if reference_hits(neg, self.items):
                continue
            cands = [
                p
                for p in self.payments
                if p.amount.is_positive
                and p.id not in self.reversed
                and p.amount.amount == -neg.amount.amount
                and p.counterparty == neg.counterparty
                and 0 <= (neg.date - p.date).days <= REVERSAL_DAYS
            ]
            if neg.reference:
                same_ref = [p for p in cands if p.reference == neg.reference]
                cands = same_ref or cands
            if len(cands) == 1:
                pos = cands[0]
                self.reversed.update({pos.id, neg.id})
                self.p_reason[pos.id].append(f"역입금(취소) {neg.id}와 상계")
                self.p_reason[neg.id].append(f"입금 {pos.id}의 취소·반환으로 처리")
            elif len(cands) > 1:
                self.p_reason[neg.id].append(
                    f"취소 대상 입금 후보 {len(cands)}건: " + ", ".join(p.id for p in cands)
                )

    # -- pass 1: reference -----------------------------------------------------
    def pass_reference(self) -> None:
        cfg = self.cfg
        for p in self.payments:
            if not self._pay_free(p):
                continue
            hits = [it for it in reference_hits(p, self.items) if self._item_free(it.id)]
            if not hits:
                continue
            ids = [it.id for it in hits]
            bad = [it for it in hits if not self._same_cp(it, p)]
            if bad:
                self.p_flag[p.id].add("conflict")
                self.p_reason[p.id].append("참조번호는 일치하나 거래처가 다름")
                for it in bad:
                    self.i_flag[it.id].add("conflict")
                    self.i_cands[it.id].add(p.id)
                    self.i_reason[it.id].append(
                        f"입금 {p.id}: 참조번호 일치, 거래처 불일치({p.counterparty})"
                    )
                continue
            rem = self.book.payment_remaining(p.id)
            total = sum_money([self._open(i) for i in ids], rem.currency)
            ev = ("reference", p.reference or p.memo)
            if total.is_zero or _sign(total.amount) != _sign(rem.amount):
                self.p_reason[p.id].append("참조 대상 미결 합계의 부호가 입금과 다름")
                continue
            if abs(total.amount) <= abs(rem.amount):
                self._commit(
                    p, self._full_legs(ids, Money.zero(rem.currency)) or [], ev, "참조번호 일치"
                )
                over = rem - total
                if not over.is_zero:
                    self.p_reason[p.id].append(f"참조 대상보다 {over} 많이 입금")
                    for i in ids:
                        self.i_reason[i].append(f"입금 {p.id}이 참조 대상 합계보다 {over} 많음")
                continue
            shortfall = total - rem
            if rem.is_positive and not shortfall.is_negative and shortfall <= cfg.fee_tolerance:
                legs = self._full_legs(ids, shortfall)
                if legs is not None:
                    self._commit(p, legs, ev, "참조번호 일치")
                    continue
            if len(ids) == 1:
                self._commit(
                    p, [Leg(ids[0], rem, Money.zero(rem.currency))], ev, "참조번호 일치(부분 입금)"
                )
                continue
            res = self._search([self.item_by_id[i] for i in ids], rem)
            if res is not None and len(res.solutions) == 1 and not res.exhausted:
                chosen = [ids[k] for k in res.solutions[0]]
                # The subset sum may exceed ``rem`` by up to the fee tolerance (second search
                # round); that shortfall must be written off, never allocated.
                chosen_total = sum_money([self._open(i) for i in chosen], rem.currency)
                legs = self._full_legs(chosen, chosen_total - rem)
                if legs is not None:
                    self._commit(p, legs, ev, "참조번호 일치(일부 항목 합계)")
                    continue
                self._flag_ambiguous(
                    p, ids, "참조 대상 일부 합계와 입금액 차이를 흡수할 항목이 없음"
                )
            else:
                self._flag_ambiguous(p, ids, "참조 대상 여러 건 중 입금 대상 특정 불가")

    # -- pass 2: one-to-one amount ---------------------------------------------
    def pass_one_to_one(self) -> bool:
        tol = self.cfg.fee_tolerance
        free_items = [it for it in self.items if self._item_free(it.id)]
        p_c: dict[str, tuple[list[str], bool]] = {}
        for p in self.payments:
            if not self._pay_free(p) or not self.book.is_untouched(p.id):
                continue
            rem = self.book.payment_remaining(p.id)
            near = [it for it in free_items if self._same_cp(it, p) and in_window(it, p, self.cfg)]
            exact = [it.id for it in near if self._open(it.id) == rem]
            if exact:
                p_c[p.id] = (exact, False)
                continue
            if rem.is_positive and tol.is_positive:
                fee = [
                    it.id
                    for it in near
                    if self._open(it.id) > rem and (self._open(it.id) - rem) <= tol
                ]
                if fee:
                    p_c[p.id] = (fee, True)
        rev: dict[str, set[str]] = {}
        for pid, (iids, _) in p_c.items():
            for iid in iids:
                rev.setdefault(iid, set()).add(pid)
        progress = False
        pays = {p.id: p for p in self.payments}
        for pid in sorted(p_c, key=lambda x: (pays[x].date, x)):
            iids, is_fee = p_c[pid]
            p = pays[pid]
            if len(iids) == 1 and rev[iids[0]] == {pid}:
                iid = iids[0]
                rem = self.book.payment_remaining(pid)
                shortfall = self._open(iid) - rem
                self._commit(
                    p,
                    [Leg(iid, rem, shortfall)],
                    ("amount", "date_window"),
                    "금액 일치(허용 오차 내)" if is_fee else "금액·기간 일치",
                )
            else:
                self._flag_ambiguous(p, iids, "같은 금액의 후보가 여러 건")
                for iid in iids:
                    for other in rev[iid]:
                        if other != pid:
                            self._flag_ambiguous(pays[other], [iid], "같은 금액의 후보가 여러 건")
            progress = True
        return progress

    # -- pass 3: subset-sum -------------------------------------------------------
    def _search(self, cands: list[Item], rem: Money) -> SubsetSearch | None:
        cfg = self.cfg
        vals = [self._open(it.id).amount for it in cands]
        self.stats["subset_searches"] += 1

        def run(hi: int) -> SubsetSearch:
            return find_subsets(
                vals,
                rem.amount,
                hi,
                max_size=cfg.max_subset_size,
                max_nodes=cfg.max_nodes,
                time_limit_s=cfg.time_limit_s,
            )

        res = run(rem.amount)
        self.stats["subset_nodes"] += res.nodes
        if not res.solutions and not res.exhausted and cfg.fee_tolerance.is_positive:
            res = run(rem.amount + cfg.fee_tolerance.amount)
            self.stats["subset_nodes"] += res.nodes
        return res

    def pass_subset(self) -> bool:
        cfg = self.cfg
        progress = False
        for p in self.payments:
            if not self._pay_free(p) or not self.book.is_untouched(p.id):
                continue
            rem = self.book.payment_remaining(p.id)
            if not rem.is_positive:
                continue
            cands = [
                it
                for it in self.items
                if self._item_free(it.id) and self._same_cp(it, p) and in_window(it, p, cfg)
            ]
            has_credit = any(self._open(it.id).is_negative for it in cands)
            if not has_credit:
                cands = [
                    it
                    for it in cands
                    if self._open(it.id).amount <= rem.amount + cfg.fee_tolerance.amount
                ]
            if len(cands) < 2:
                continue
            if len(cands) > cfg.max_candidates:
                self._flag_ambiguous(
                    p,
                    [it.id for it in cands],
                    f"합산 후보 {len(cands)}건이 상한 {cfg.max_candidates}건 초과",
                )
                progress = True
                continue
            res = self._search(cands, rem)
            if res is None:
                continue
            if res.exhausted:
                self._flag_ambiguous(p, [it.id for it in cands], "합산 탐색 상한 도달")
                progress = True
            elif len(res.solutions) == 1:
                chosen = [cands[k].id for k in res.solutions[0]]
                total = sum_money([self._open(i) for i in chosen], rem.currency)
                legs = self._full_legs(chosen, total - rem)
                if legs is None:
                    continue
                self._commit(p, legs, ("subset_sum",), f"여러 건 합산 일치({len(chosen)}건)")
                progress = True
            elif len(res.solutions) > 1:
                ids = {cands[k].id for sol in res.solutions for k in sol}
                self._flag_ambiguous(p, ids, "합계가 같은 조합이 여러 개")
                progress = True
        return progress

    # -- pass 4: partial ------------------------------------------------------------
    def pass_partial(self) -> bool:
        progress = False
        for p in self.payments:
            if not self._pay_free(p) or not self.book.is_untouched(p.id):
                continue
            rem = self.book.payment_remaining(p.id)
            if not rem.is_positive:
                continue
            cands = [
                it.id
                for it in self.items
                if self._item_free(it.id)
                and self._same_cp(it, p)
                and in_window(it, p, self.cfg)
                and self._open(it.id) > rem
            ]
            if len(cands) == 1:
                self._commit(
                    p,
                    [Leg(cands[0], rem, Money.zero(rem.currency))],
                    ("amount_partial",),
                    "부분 입금",
                )
                progress = True
            elif len(cands) > 1:
                self._flag_ambiguous(p, cands, "부분 입금 대상 후보가 여러 건")
                progress = True
        return progress

    # -- result -----------------------------------------------------------------------
    def run(self) -> ReconResult:
        self.pass_reversals()
        self.pass_reference()
        for _ in range(len(self.payments) + 1):
            changed = self.pass_one_to_one()
            changed = self.pass_subset() or changed
            changed = self.pass_partial() or changed
            if not changed:
                break
        allocs = self.book.allocations
        by_item: dict[str, list[Allocation]] = {}
        for a in allocs:
            by_item.setdefault(a.target_id, []).append(a)
        items: dict[str, ItemResult] = {}
        for it in self.items:
            alloc = self.book.item_allocated(it.id)
            fee = self.book.item_fee(it.id)
            open_ = self._open(it.id)
            if "conflict" in self.i_flag[it.id]:
                st = ReconcileStatus.CONFLICT
            elif "ambiguous" in self.i_flag[it.id] and not open_.is_zero:
                st = ReconcileStatus.AMBIGUOUS
            elif open_.is_zero:
                st = ReconcileStatus.MATCHED
            elif not alloc.is_zero or not fee.is_zero:
                st = ReconcileStatus.PARTIAL
            else:
                st = ReconcileStatus.UNMATCHED
            items[it.id] = ItemResult(
                it.id,
                st,
                it.amount,
                alloc,
                fee,
                open_,
                tuple(by_item.get(it.id, [])),
                tuple(sorted(self.i_cands[it.id])),
                tuple(dict.fromkeys(self.i_reason[it.id])),
            )
        pays: dict[str, PaymentResult] = {}
        for p in self.payments:
            used = self.book.payment_used(p.id)
            if p.id in self.reversed:
                st = ReconcileStatus.MATCHED
                unapplied = Money.zero(p.amount.currency)
            else:
                unapplied = p.amount - used
                if "conflict" in self.p_flag[p.id]:
                    st = ReconcileStatus.CONFLICT
                elif "ambiguous" in self.p_flag[p.id]:
                    st = ReconcileStatus.AMBIGUOUS
                elif unapplied.is_zero:
                    st = ReconcileStatus.MATCHED
                elif not used.is_zero:
                    st = ReconcileStatus.PARTIAL
                else:
                    st = ReconcileStatus.UNMATCHED
            pays[p.id] = PaymentResult(
                p.id,
                st,
                p.amount,
                used,
                unapplied,
                tuple(sorted(self.p_cands[p.id])),
                tuple(dict.fromkeys(self.p_reason[p.id])),
            )
        return ReconResult(items, pays, allocs, dict(self.stats))


def reconcile(
    items: Iterable[Item], payments: Iterable[Payment], cfg: ReconConfig | None = None
) -> ReconResult:
    return _Run(items, payments, cfg or ReconConfig()).run()
