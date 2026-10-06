"""Properties of recomputation over document rows and economic receivables.

- incremental recompute == full recompute on the same snapshot (and same graph), with
  settlement lines, invoices linked to them by reference, user link confirmations and their
  removal in the change stream;
- upload order does not change the result;
- adding a document that is duplicate evidence of an existing sale (an invoice with the
  counterparty, reference and amount of a uniquely referenced settlement line) never
  increases the counted receivable total or the open balance.
"""

from datetime import date, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jt_unit_helpers import T, agreement, fact, inv, line, snapshot, txn

from jettae.domain import Change, ChangeKind, Invoice, LineKind, Money, SettlementLine, TradeType
from jettae.domain.models import EvidenceLink, LinkRelation
from jettae.evidence import full_recompute, incremental_recompute
from jettae.recon.candidates import normalize_counterparty, normalize_ref

CPS = ["가나", "다라", "마바"]
AMTS = [100_000, 200_000, 300_000, 500_000]
D0 = date(2025, 3, 1)
PO_REFS = ["P-1001", "P-1002", "P-1003"]  # shared by settlement lines and invoices


@st.composite
def invoices(draw, prefix="I"):
    n = draw(st.integers(1, 5))
    out = []
    for k in range(n):
        out.append(
            inv(
                f"{prefix}{k}",
                draw(st.sampled_from(AMTS)),
                cp=draw(st.sampled_from(CPS)),
                received=draw(st.one_of(st.none(), st.just(D0 + timedelta(days=k * 9)))),
                trade=draw(st.sampled_from([TradeType.DIRECT, TradeType.CONSIGNMENT, None])),
                ref=draw(st.one_of(st.none(), st.just(f"R-{prefix}{k}"), st.sampled_from(PO_REFS))),
                issue=D0 + timedelta(days=k * 9),
            )
        )
    return out


def a_line(draw, lid):
    return line(
        lid,
        draw(st.sampled_from([*AMTS, -50_000])),
        ref=draw(st.one_of(st.none(), st.sampled_from(PO_REFS))),
        cp=draw(st.sampled_from(CPS)),
        received=draw(st.one_of(st.none(), st.just(D0 + timedelta(days=draw(st.integers(0, 40)))))),
        trade=draw(st.sampled_from([TradeType.DIRECT, None])),
        kind=draw(st.sampled_from([LineKind.SALE, LineKind.SALE, LineKind.DEDUCTION])),
    )


def a_link(draw, lid, snap):
    invoice = draw(st.sampled_from(snap.invoices))
    if snap.settlement_lines and draw(st.booleans()):
        target = draw(st.sampled_from(snap.settlement_lines))
        return EvidenceLink(lid, T, invoice.id, LinkRelation.SAME_SALE, target.id)
    return EvidenceLink(lid, T, invoice.id, LinkRelation.SEPARATE_SALE)


def dup_of(src: SettlementLine, iid: str) -> Invoice:
    """An invoice evidencing the same sale as settlement line ``src``."""
    return Invoice(
        id=iid,
        tenant_id=T,
        counterparty=src.counterparty,
        amount=src.amount,
        reference=src.reference,
        trade_type=src.trade_type,
        goods_received_date=src.goods_received_date,
        facts=(f"f-{iid}",),
    )


def a_txn(draw, tid):
    return txn(
        tid,
        draw(st.sampled_from([100_000, 200_000, 300_000, 400_000, 500_000, 700_000, -200_000])),
        D0 + timedelta(days=draw(st.integers(30, 120))),
        cp=draw(st.sampled_from([*CPS, None])),
        memo=draw(st.sampled_from(["", "R-I0", "R-I1", "R-I2", *PO_REFS])),
    )


OPS = [
    "add_txn",
    "rm_txn",
    "upd_inv",
    "add_ag",
    "rm_ag",
    "add_inv",
    "rm_inv",
    "upd_fact",
    "add_line",
    "rm_line",
    "upd_line",
    "add_dup_inv",
    "add_link",
    "rm_link",
]


@st.composite
def scenario(draw):
    invs = draw(invoices())
    txns = [a_txn(draw, f"T{k}") for k in range(draw(st.integers(0, 4)))]
    ags = [
        agreement(
            f"A{k}",
            cp=draw(st.sampled_from(CPS)),
            rollover=draw(st.sampled_from([True, False, None])),
        )
        for k in range(draw(st.integers(0, 2)))
    ]
    lines = [a_line(draw, f"L{k}") for k in range(draw(st.integers(0, 3)))]
    s0 = snapshot(invoices=invs, txns=txns, agreements=ags, lines=lines, as_of=date(2025, 9, 1))
    steps = []
    snap = s0
    for step in range(draw(st.integers(1, 5))):
        op = draw(st.sampled_from(OPS))
        ch = None
        if op == "add_txn":
            t = a_txn(draw, f"N{step}")
            ch = [
                Change(ChangeKind.ADD, "fact", f"f-{t.id}", fact(f"f-{t.id}", t.id, 1)),
                Change(ChangeKind.ADD, "bank_txn", t.id, t),
            ]
        elif op == "rm_txn" and snap.bank_txns:
            t = draw(st.sampled_from(snap.bank_txns))
            ch = [Change(ChangeKind.REMOVE, "bank_txn", t.id)]
        elif op == "upd_inv" and snap.invoices:
            i: Invoice = draw(st.sampled_from(snap.invoices))
            new = Invoice(**{**i.__dict__, "amount": Money(draw(st.sampled_from(AMTS)))})
            ch = [Change(ChangeKind.UPDATE, "invoice", i.id, new)]
        elif op == "add_ag":
            a = agreement(
                f"NA{step}",
                cp=draw(st.sampled_from(CPS)),
                rollover=draw(st.sampled_from([True, False])),
            )
            ch = [Change(ChangeKind.ADD, "agreement", a.id, a)]
        elif op == "rm_ag" and snap.agreements:
            a = draw(st.sampled_from(snap.agreements))
            ch = [Change(ChangeKind.REMOVE, "agreement", a.id)]
        elif op == "add_inv":
            i = draw(invoices(prefix=f"X{step}_"))[0]
            ch = [Change(ChangeKind.ADD, "invoice", i.id, i)]
        elif op == "rm_inv" and len(snap.invoices) > 1:
            i = draw(st.sampled_from(snap.invoices))
            ch = [Change(ChangeKind.REMOVE, "invoice", i.id)]
        elif op == "add_line":
            ln = a_line(draw, f"NL{step}")
            ch = [Change(ChangeKind.ADD, "settlement_line", ln.id, ln)]
        elif op == "rm_line" and snap.settlement_lines:
            ln = draw(st.sampled_from(snap.settlement_lines))
            ch = [Change(ChangeKind.REMOVE, "settlement_line", ln.id)]
        elif op == "upd_line" and snap.settlement_lines:
            ln = draw(st.sampled_from(snap.settlement_lines))
            new_ln = SettlementLine(**{**ln.__dict__, "amount": Money(draw(st.sampled_from(AMTS)))})
            ch = [Change(ChangeKind.UPDATE, "settlement_line", ln.id, new_ln)]
        elif op == "add_dup_inv" and snap.settlement_lines:
            d = dup_of(draw(st.sampled_from(snap.settlement_lines)), f"D{step}")
            ch = [Change(ChangeKind.ADD, "invoice", d.id, d)]
        elif op == "add_link" and snap.invoices:
            lk = a_link(draw, f"K{step}", snap)
            ch = [Change(ChangeKind.ADD, "evidence_link", lk.id, lk)]
        elif op == "rm_link" and snap.evidence_links:
            lk = draw(st.sampled_from(snap.evidence_links))
            ch = [Change(ChangeKind.REMOVE, "evidence_link", lk.id)]
        elif op == "upd_fact" and snap.facts:
            f = draw(st.sampled_from(snap.facts))
            nf = fact(f.id, f.subject_id or "", draw(st.integers(0, 9)), doc="docv-2")
            ch = [Change(ChangeKind.UPDATE, "fact", f.id, nf)]
        if ch:
            snap = snap.apply_all(ch)
            steps.append(ch)
    return s0, steps


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(scenario())
def test_incremental_equals_full(sc):
    s0, steps = sc
    snap = s0
    prev = full_recompute(snap)
    for changes in steps:
        snap = snap.apply_all(changes)
        inc, plan, groups = incremental_recompute(prev, snap, changes)
        full = full_recompute(snap)
        assert not plan.fallback_full
        assert inc.comparable() == full.comparable()
        assert inc.graph.to_dict() == full.graph.to_dict()
        assert groups <= set(snap.receivable_groups) | set(prev.groups)
        prev = inc
    assert prev.tenant_id == T


def _open_balance(result) -> int:
    return sum(d.computation("recon").outputs["open"].amount for d in result.decisions.values())


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(scenario())
def test_duplicate_evidence_never_increases_receivables(sc):
    s0, steps = sc
    snap = s0
    for changes in steps:
        snap = snap.apply_all(changes)
    base_total = snap.counted_total()
    base_open = _open_balance(full_recompute(snap))
    sale = [ln for ln in snap.settlement_lines if ln.line_kind is LineKind.SALE]
    for k, ln in enumerate(sale):
        key = (normalize_counterparty(ln.counterparty), normalize_ref(ln.reference))
        same = [
            o
            for o in sale
            if (normalize_counterparty(o.counterparty), normalize_ref(o.reference)) == key
        ]
        if not key[1] or len(same) != 1:
            continue  # only an explicit, unique reference identifies the same sale
        dup = dup_of(ln, f"DUP{k}")
        after = snap.apply(Change(ChangeKind.ADD, "invoice", dup.id, dup))
        assert after.counted_total() <= base_total
        assert dup.id not in after.receivables  # corroborating evidence, not a receivable
        assert _open_balance(full_recompute(after)) <= base_open


@st.composite
def documents(draw):
    docs: list[Change] = []
    for k in range(draw(st.integers(1, 3))):
        ln = a_line(draw, f"L{k}")
        docs.append(Change(ChangeKind.ADD, "settlement_line", ln.id, ln))
        if draw(st.booleans()):
            d = dup_of(ln, f"D{k}")
            docs.append(Change(ChangeKind.ADD, "invoice", d.id, d))
    for i in draw(invoices()):
        docs.append(Change(ChangeKind.ADD, "invoice", i.id, i))
    for k in range(draw(st.integers(0, 3))):
        t = a_txn(draw, f"T{k}")
        docs.append(Change(ChangeKind.ADD, "bank_txn", t.id, t))
    if draw(st.booleans()):
        inv_ids = [c.entity_id for c in docs if c.entity == "invoice"]
        line_ids = [c.entity_id for c in docs if c.entity == "settlement_line"]
        lk = EvidenceLink(
            "K0",
            T,
            draw(st.sampled_from(inv_ids)),
            LinkRelation.SAME_SALE,
            draw(st.sampled_from(line_ids)),
        )
        docs.append(Change(ChangeKind.ADD, "evidence_link", lk.id, lk))
    return docs, draw(st.permutations(docs)), draw(st.permutations(docs))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(documents())
def test_upload_order_does_not_change_results(sc):
    _, first, second = sc
    finals = []
    for order in (first, second):
        snap = snapshot(as_of=date(2025, 9, 1))
        prev = full_recompute(snap)
        for c in order:
            snap = snap.apply(c)
            prev, plan, _ = incremental_recompute(prev, snap, [c])
            assert not plan.fallback_full
            assert prev.comparable() == full_recompute(snap).comparable()
        finals.append((prev.comparable(), snap.counted_total()))
    assert finals[0] == finals[1]
