"""Per purchase-order item: map BPI 2019 events to domain records and link them with the
deterministic matcher (``jettae.recon``).

- goods receipts (``Record Goods Receipt``; ``Cancel Goods Receipt`` as a credit) are linked
  to invoice receipts (``Record Invoice Receipt``; ``Cancel Invoice Receipt`` as a negative
  entry) -> GR<->IR linking;
- invoice receipts become domain :class:`Invoice` records (``issue_date`` = IR date,
  ``goods_received_date`` = latest linked GR date) and ``Clear Invoice`` events become
  :class:`BankTxn` records -> IR<->clear linking.

The PO item (trace) is the linking scope, so no cross-item guessing happens. Equal amounts
that cannot be told apart stay AMBIGUOUS (no FIFO guess). No Korean statutory deadline is
applied to this data (SPEC §3).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from jettae.domain.models import BankTxn, Invoice
from jettae.domain.money import Money, sum_money
from jettae.domain.status import ReconcileStatus
from jettae.recon import (
    Item,
    Payment,
    ReconConfig,
    ReconResult,
    item_from_invoice,
    normalize_counterparty,
    payment_from_txn,
    reconcile,
)
from jettae.sources.bpi2019.convert import (
    ACT_CLEAR,
    ACT_CREATE,
    ACT_GR,
    ACT_GR_CANCEL,
    ACT_IR,
    ACT_IR_CANCEL,
    KEY_GR_BASED,
    PoItem,
    amount_minor,
)

TENANT = "bpi2019"
CURRENCY = "EUR"


def recon_config() -> ReconConfig:
    """Wide date windows (invoices may precede goods receipts); node cap is the
    deterministic bound, the time cap is set high so results do not depend on CPU speed."""
    return ReconConfig(
        days_before=3660,
        days_after=3660,
        fee_tolerance=Money.zero(CURRENCY),
        max_candidates=24,
        max_subset_size=6,
        max_nodes=50_000,
        time_limit_s=120.0,
    )


@dataclass(frozen=True)
class Ev:
    id: str
    day: date
    amount: Money


@dataclass
class ItemAnalysis:
    item_id: str
    vendor: str
    category: str
    gr_based_iv: str | None
    n_gr: int = 0
    n_gr_cancel: int = 0
    n_ir: int = 0
    n_ir_cancel: int = 0
    n_clear: int = 0
    create_amount: Money | None = None
    gr_total: Money = field(default_factory=lambda: Money.zero(CURRENCY))
    ir_total: Money = field(default_factory=lambda: Money.zero(CURRENCY))
    clear_total: Money = field(default_factory=lambda: Money.zero(CURRENCY))
    missing_amount: int = 0  # relevant events without a parsable amount
    missing_time: int = 0
    rounded_amounts: int = 0
    linked: bool = False  # event-level linking ran (False: skipped, too many events)
    gr_status: Counter[str] = field(default_factory=Counter)  # GR item statuses
    ir_status: Counter[str] = field(default_factory=Counter)  # IR (as invoice) statuses
    clear_status: Counter[str] = field(default_factory=Counter)  # clear (as payment) statuses
    pay_days_from_ir: list[int] = field(default_factory=list)  # per IR<->clear allocation
    pay_days_from_gr: list[int] = field(default_factory=list)
    gr_ir_conserved: bool = True
    ir_clear_conserved: bool = True
    invoices: list[Invoice] = field(default_factory=list)
    payments: list[BankTxn] = field(default_factory=list)

    @property
    def has_gr(self) -> bool:
        return self.n_gr > 0

    @property
    def has_ir(self) -> bool:
        return self.n_ir > 0

    @property
    def has_clear(self) -> bool:
        return self.n_clear > 0

    @property
    def gr_ir_diff(self) -> Money:
        return self.ir_total - self.gr_total

    @property
    def ir_clear_diff(self) -> Money:
        return self.clear_total - self.ir_total


def _events(item: PoItem, activity: str, prefix: str, a: ItemAnalysis) -> list[Ev]:
    out: list[Ev] = []
    for i, e in enumerate(item.of(activity)):
        m, rounded = amount_minor(e.amount)
        d = e.day
        if m is None:
            a.missing_amount += 1
            continue
        if d is None:
            a.missing_time += 1
            continue
        a.rounded_amounts += int(rounded)
        out.append(Ev(f"{prefix}{i:04d}", d, m))
    return out


def _conserved(res: ReconResult, pay_amounts: dict[str, Money]) -> bool:
    used: dict[str, Money] = {}
    for al in res.allocations:
        used[al.source_id] = used.get(al.source_id, Money.zero(al.amount.currency)) + al.amount
    for pid, u in used.items():
        amt = pay_amounts[pid]
        if (amt.is_positive and (u > amt or u.is_negative)) or (
            amt.is_negative and (u < amt or u.is_positive)
        ):
            return False
    return True


def analyze_item(
    item: PoItem, cfg: ReconConfig | None = None, max_events: int = 400
) -> ItemAnalysis:
    cfg = cfg or recon_config()
    a = ItemAnalysis(
        item.id,
        item.vendor,
        item.category,
        item.attrs.get(KEY_GR_BASED),
    )
    creates = item.of(ACT_CREATE)
    if creates:
        a.create_amount, _ = amount_minor(creates[0].amount)
    gr = _events(item, ACT_GR, "gr", a)
    grc = _events(item, ACT_GR_CANCEL, "grc", a)
    ir = _events(item, ACT_IR, "ir", a)
    irc = _events(item, ACT_IR_CANCEL, "irc", a)
    cl = _events(item, ACT_CLEAR, "cl", a)
    a.n_gr, a.n_gr_cancel, a.n_ir, a.n_ir_cancel, a.n_clear = (
        len(item.of(ACT_GR)),
        len(item.of(ACT_GR_CANCEL)),
        len(item.of(ACT_IR)),
        len(item.of(ACT_IR_CANCEL)),
        len(item.of(ACT_CLEAR)),
    )
    a.gr_total = sum_money([e.amount for e in gr], CURRENCY) - sum_money(
        [e.amount for e in grc], CURRENCY
    )
    a.ir_total = sum_money([e.amount for e in ir], CURRENCY) - sum_money(
        [e.amount for e in irc], CURRENCY
    )
    a.clear_total = sum_money([e.amount for e in cl], CURRENCY)

    if len(gr) + len(grc) + len(ir) + len(irc) + len(cl) > max_events:
        return a
    a.linked = True
    cp = normalize_counterparty(item.vendor) or "vendor"

    # ---- GR <-> IR (GR = receivable-like item, IR = settling entry) ----------------
    gr_items = [Item(e.id, cp, e.amount, e.day) for e in gr] + [
        Item(e.id, cp, -e.amount, e.day) for e in grc
    ]
    ir_pays = [Payment(e.id, cp, e.amount, e.day) for e in ir] + [
        Payment(e.id, cp, -e.amount, e.day) for e in irc
    ]
    linked_gr: dict[str, date] = {}
    if gr_items and ir_pays:
        r1 = reconcile(gr_items, ir_pays, cfg)
        a.gr_ir_conserved = _conserved(r1, {p.id: p.amount for p in ir_pays})
        gr_day = {e.id: e.day for e in gr}
        for e in gr:
            a.gr_status[r1.items[e.id].status.value] += 1
        for al in r1.allocations:
            if al.target_id in gr_day and al.amount.is_positive:
                d = gr_day[al.target_id]
                prev = linked_gr.get(al.source_id)
                linked_gr[al.source_id] = d if prev is None or d > prev else prev
    elif gr_items:
        a.gr_status[ReconcileStatus.UNMATCHED.value] += len(gr)

    # ---- IR (domain Invoice) <-> Clear Invoice (domain BankTxn) ---------------------
    invoices = [
        Invoice(
            id=e.id,
            tenant_id=TENANT,
            counterparty=cp,
            amount=e.amount,
            issue_date=e.day,
            reference=None,
            goods_received_date=linked_gr.get(e.id),
        )
        for e in ir
    ] + [
        Invoice(id=e.id, tenant_id=TENANT, counterparty=cp, amount=-e.amount, issue_date=e.day)
        for e in irc
    ]
    txns = [
        BankTxn(id=e.id, tenant_id=TENANT, booked_date=e.day, amount=e.amount, counterparty=cp)
        for e in cl
    ]
    a.invoices, a.payments = invoices, txns
    if invoices and txns:
        r2 = reconcile(
            [item_from_invoice(v) for v in invoices], [payment_from_txn(t) for t in txns], cfg
        )
        a.ir_clear_conserved = _conserved(r2, {t.id: t.amount for t in txns})
        inv_by_id = {v.id: v for v in invoices}
        for e in ir:
            a.ir_status[r2.items[e.id].status.value] += 1
        for t in txns:
            a.clear_status[r2.payments[t.id].status.value] += 1
        txn_day = {t.id: t.booked_date for t in txns}
        for al in r2.allocations:
            inv = inv_by_id[al.target_id]
            if not al.amount.is_positive or inv.issue_date is None:
                continue
            paid = txn_day[al.source_id]
            a.pay_days_from_ir.append((paid - inv.issue_date).days)
            if inv.goods_received_date is not None:
                a.pay_days_from_gr.append((paid - inv.goods_received_date).days)
    else:
        if ir:
            a.ir_status[ReconcileStatus.UNMATCHED.value] += len(ir)
        if cl:
            a.clear_status[ReconcileStatus.UNMATCHED.value] += len(cl)
    return a
