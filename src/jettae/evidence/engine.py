"""Deterministic analysis engine: snapshot -> decisions + dependency graph.

Reconciliation couples all receivables and payments of one counterparty, so the unit of
recomputation is a counterparty *group*. Every decision records the scopes of its group
(receivables, attributed bank transactions, agreements) and the analysis config, plus
direct edges from documents/facts/records. ``incremental_recompute`` recomputes only the
groups that the invalidation planner marks and must equal ``full_recompute`` on the same
snapshot (property-tested).

One decision per *economic receivable* (``Snapshot.economic_receivables``), not per document
row: a tax invoice explicitly linked to a settlement line is corroborating evidence inside
the line's decision (computation ``evidence``) and adds no amount. Payments are allocated
only to established receivables. A document whose link is uncertain gets its own AMBIGUOUS
decision with the confirmation item ``evidence_link``; it is not reconciled, so its
``recon.open`` is zero and ``evidence.counted`` is false. Linked documents with different
amounts make the decision CONFLICT (both amounts are shown; no due date is computed).

An agreement's ``payment_term_days`` yields a separate ``contractual_due`` computation (date
only, never interest at the statutory rate; see :mod:`jettae.rules.contract_term`).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from jettae.domain.hashing import content_hash, to_canonical
from jettae.domain.models import (
    Agreement,
    Allocation,
    BankTxn,
    Change,
    Computation,
    Decision,
    DocumentRef,
    Invoice,
    PendingKind,
    Receivable,
    ReceivableDocument,
    RecomputePlan,
    SettlementLine,
    SourceSpan,
)
from jettae.domain.money import Money, sum_money
from jettae.domain.status import LineKind, ReconcileStatus, TradeType
from jettae.evidence.deps import DependencyGraph, node_comp, node_dec, node_doc, node_fact, node_rec
from jettae.evidence.invalidate import plan as plan_invalidation
from jettae.evidence.snapshot import QueryScope, Snapshot, decision_id_for
from jettae.recon.candidates import (
    Item,
    item_from_invoice,
    item_from_line,
    normalize_ref,
    payment_from_txn,
)
from jettae.recon.matcher import ItemResult, PaymentResult, reconcile
from jettae.rules.contract_term import (
    CONTRACT_TERM_ID,
    NO_INTEREST_NOTE,
    ContractualDue,
    compute_contractual_due,
    difference_days,
)
from jettae.rules.kr_retail import (
    BASE_FIELD,
    END_UNSET_NOTE,
    DueResult,
    Insufficient,
    compute_due,
)

AMBIGUOUS_DOCS = ("입금 대상 거래를 특정할 수 있는 송금 내역(적요·참조번호) 또는 지급 통지서",)
CONFLICT_DOCS = ("서로 다른 조건이 적힌 약정서·계약서의 최신본",)
LINK_DOCS = (
    "같은 거래인지 확인할 수 있는 자료(발주번호·정산번호가 함께 적힌 세금계산서 또는 정산서)",
)
AMOUNT_CONFLICT_DOCS = ("금액이 서로 다른 정산서·세금계산서의 정정본 또는 차이 내역",)
DUPLICATE_DOCS = (
    "같은 정산서를 다시 받은 것인지 확인할 수 있는 자료(정산서 재발행·정정 여부, 또는 서로 다른 "
    "거래임을 보여 주는 발주·입고 기록)",
)
DIRECTION_DOCS = (
    "매출 세금계산서인지 확인할 수 있는 정보(우리 회사 사업자등록번호 또는 매출/매입 구분을 열 "
    "매핑에서 지정)",
    "공급받는자(거래처) 상호가 적힌 세금계산서 목록",
)
PENDING_DOCS: dict[PendingKind, tuple[str, ...]] = {
    PendingKind.EVIDENCE_LINK: LINK_DOCS,
    PendingKind.DUPLICATE_LINE: DUPLICATE_DOCS,
    PendingKind.INVOICE_DIRECTION: DIRECTION_DOCS,
}
# choices offered by POST /decisions/{id}/evidence-link for each pending kind
PENDING_CHOICES: dict[PendingKind, list[str]] = {
    PendingKind.EVIDENCE_LINK: ["same_sale", "separate_sale"],
    PendingKind.DUPLICATE_LINE: ["same_sale", "separate_sale"],
    PendingKind.INVOICE_DIRECTION: [],  # resolved by re-reading the file with the direction
}
UNRESOLVED_LINK = "evidence_link"  # confirmation item: is this document the same sale?
UNRESOLVED_AMOUNT = "evidence_amount_conflict"
UNRESOLVED_TERM_CONFLICT = "contract_term_conflict"
DOC_LABEL = {"invoice": "세금계산서", "settlement_line": "정산 행"}


@dataclass(frozen=True)
class GroupOutcome:
    group: str
    decision_ids: tuple[str, ...]
    payments: Mapping[str, PaymentResult]


@dataclass(frozen=True)
class AnalysisResult:
    tenant_id: str
    snapshot_hash: str
    decisions: Mapping[str, Decision]
    groups: Mapping[str, GroupOutcome]
    unattributed: tuple[tuple[str, str], ...]
    graph: DependencyGraph = field(compare=False, repr=False)

    def comparable(self) -> Any:
        """Canonical content for equality checks (full vs incremental)."""
        return to_canonical(
            {
                "snapshot_hash": self.snapshot_hash,
                "decisions": dict(sorted(self.decisions.items())),
                "groups": dict(sorted(self.groups.items())),
                "unattributed": self.unattributed,
            }
        )

    def decision_group(self) -> dict[str, str]:
        return {d: g.group for g in self.groups.values() for d in g.decision_ids}


# --------------------------------------------------------------------------- helpers
def _entity(rec: Invoice | SettlementLine) -> str:
    return "invoice" if isinstance(rec, Invoice) else "settlement_line"


def _ref_date(rec: Invoice | SettlementLine) -> date | None:
    if isinstance(rec, Invoice):
        return rec.goods_received_date or rec.sales_close_date or rec.issue_date
    return rec.sales_close_date or rec.goods_received_date or rec.period_end


def _applicable(agreements: Iterable[Agreement], on: date | None) -> list[Agreement]:
    out = []
    for a in agreements:
        if on is not None:
            if a.valid_from is not None and on < a.valid_from:
                continue
            if a.valid_to is not None and on > a.valid_to:
                continue
        out.append(a)
    return out


def _one(values: Iterable[Any]) -> tuple[Any, bool]:
    """(single value or None, conflict?) over non-None values."""
    vals = sorted({v for v in values if v is not None}, key=str)
    if len(vals) > 1:
        return None, True
    return (vals[0] if vals else None), False


@dataclass(frozen=True)
class _DueOut:
    computation: Computation
    head: DueResult | Insufficient
    tranche_notes: tuple[str, ...] = ()
    uses_as_of: bool = False  # outputs depend on the reference date (open tranche)


def _due_block(
    rec: Invoice | SettlementLine,
    ir: ItemResult,
    trade_type: TradeType | None,
    rollover: bool | None,
    rounding: Any,
    monthly: bool | None,
    pay_dates: Mapping[str, date],
    snap: Snapshot,
    *,
    withhold_delay: bool = False,
) -> _DueOut:
    """Due date per variant, plus delay days / interest per tranche.

    Tranches: each allocated amount ends at its payment date; the open amount ends at
    ``as_of``. Only decisions with an open tranche depend on ``as_of`` (``uses_as_of``).
    With ``withhold_delay`` (allocation not determined, e.g. AMBIGUOUS) only the due date is
    computed: delay days and interest stay ``None`` because they depend on which payment
    settles the item.
    """
    cfg = snap.config
    base = getattr(rec, BASE_FIELD[trade_type]) if trade_type is not None else None
    tax_date = rec.issue_date if isinstance(rec, Invoice) else None
    common = dict(
        rollover=rollover,
        rounding=rounding,
        calendar=cfg.calendar,
        registry=cfg.registry,
        known_at=cfg.known_at,
        monthly_settlement=monthly,
    )
    head = compute_due(trade_type, base, rec.amount, tax_invoice_date=tax_date, **common)
    inputs: dict[str, Any] = {
        "trade_type": trade_type,
        "base_date": base,
        "rollover": rollover,
        "rounding": rounding,
        "principal": rec.amount,
    }
    if isinstance(head, Insufficient):
        return _DueOut(
            Computation(
                "due",
                None,
                inputs,
                {"insufficient": True, "missing": head.missing, "notes": head.notes},
            ),
            head,
        )
    # tranches: allocated amounts end at their payment date; open amount at as_of
    tranches: list[tuple[str, Money, date | None]] = []
    allocs = sorted(ir.allocations, key=lambda a: (pay_dates[a.source_id], a.source_id))
    for a in allocs:
        tranches.append((a.source_id, a.amount, pay_dates[a.source_id]))
    uses_as_of = False
    if not ir.fee_difference.is_zero:
        if allocs:
            last: date | None = pay_dates[allocs[-1].source_id]
        else:
            last, uses_as_of = cfg.as_of, True
        tranches.append(("fee_difference", ir.fee_difference, last))
    if ir.open.is_positive:
        tranches.append(("unpaid", ir.open, None))
        uses_as_of = True
    notes: list[str] = []
    if withhold_delay:
        tranches = []
        uses_as_of = False
        notes.append(
            "입금 대상이 특정되지 않아 지연일수·지연이자를 계산하지 않음"
            " (확인이 필요한 조건: 입금 배분)"
        )
    else:
        for src, amt, end in tranches:
            if src == "unpaid":
                notes.append(
                    f"미지급 {amt}: 기준일(as_of) {cfg.as_of.isoformat()}까지 지연일수 계산"
                    if cfg.as_of is not None
                    else f"미지급 {amt}: 기준일(as_of) 미지정, 지연일수 0으로 표시"
                )
            elif src == "fee_difference":
                notes.append(
                    f"허용 오차로 처리한 차액 {amt}: "
                    + (f"{end.isoformat()}까지 지연일수 계산" if end else "종료일 미지정")
                )
            else:
                notes.append(f"입금 [{src}] {amt}: 입금일 {end.isoformat() if end else '-'}까지")
    if uses_as_of:
        inputs["as_of"] = cfg.as_of
    variants_out = []
    for v in head.variants:
        rows: list[dict[str, Any]] = []
        total: Money | None = Money.zero(rec.amount.currency)
        for src, amt, end in tranches:
            r = compute_due(
                trade_type,
                base,
                amt,
                paid_date=end if src != "unpaid" else None,
                as_of=cfg.as_of if src == "unpaid" else None,
                **{**common, "rollover": v.rollover},
            )
            assert isinstance(r, DueResult)
            rv = r.variants[0]
            rows.append(
                {
                    "source": src,
                    "amount": amt,
                    "end_date": r.end_date,
                    "delay_days": rv.delay_days,
                    "interest": rv.interest,
                }
            )
            total = None if (total is None or rv.interest is None) else total + rv.interest
        variants_out.append(
            {
                "label": v.label,
                "rollover": v.rollover,
                "due_date": v.due_date,
                "max_delay_days": (
                    None if withhold_delay else max((r["delay_days"] for r in rows), default=0)
                ),
                "interest_total": None if withhold_delay else total,
                "tranches": rows,
            }
        )
    outputs: dict[str, Any] = {
        "insufficient": False,
        "base_date": head.base_date,
        "term_days": head.term_days,
        "interest_rule_version": head.interest_rule_version,
        "variants": variants_out,
        "unresolved": head.unresolved,
        "source_urls": head.source_urls,
    }
    if withhold_delay:
        outputs["delay_withheld"] = "allocation"
    return _DueOut(
        Computation("due", head.rule_version, inputs, outputs), head, tuple(notes), uses_as_of
    )


def _link(
    snap: Snapshot,
    edges: list[tuple[str, str]],
    facts_used: list[str],
    node: str,
    fact_ids: Iterable[str],
    targets: list[str],
) -> None:
    """Record edges fact->record->targets and doc->fact for one record."""
    for t in targets:
        edges.append((node, t))
    for fid in fact_ids:
        facts_used.append(fid)
        edges.append((node_fact(fid), node))
        f = snap.fact_index.get(fid)
        if f is not None and f.span is not None:
            edges.append((node_doc(f.span.doc_version_id), node_fact(fid)))


def _item_for(rec: Receivable, snap: Snapshot) -> Item:
    """Reconciliation item of an economic receivable: the basis document, plus the
    references of its corroborating documents (a payment may quote the invoice number)."""
    doc = snap.documents[rec.basis_id]
    base = item_from_invoice(doc) if isinstance(doc, Invoice) else item_from_line(doc)
    alt = {normalize_ref(snap.documents[e.id].reference) for e in rec.evidence}
    alt -= {"", base.reference, base.group_ref}
    return dataclasses.replace(base, alt_refs=tuple(sorted(alt)))


def _doc_out(d: DocumentRef) -> dict[str, Any]:
    return {
        "entity": d.entity,
        "id": d.id,
        "role": d.role.value,
        "method": d.method.value if d.method is not None else None,
        "amount": d.amount,
        "reason": d.reason,
    }


def _evidence_computation(rec: Receivable) -> Computation:
    """Which document is the basis of the receivable and which documents support it."""
    basis = {
        "entity": rec.basis.value,
        "id": rec.basis_id,
        "role": "basis",
        "method": None,
        "amount": rec.amount,
        "reason": "",
    }
    confirmation: dict[str, Any] | None = None
    if not rec.counted:
        confirmation = {
            "kind": rec.pending.value,
            "document_id": rec.basis_id,
            "candidate_settlement_lines": [c.id for c in rec.candidates],
            "choices": list(PENDING_CHOICES[rec.pending]),
        }
        if rec.missing:
            confirmation["missing"] = list(rec.missing)
    return Computation(
        "evidence",
        None,
        {"receivable_id": rec.id},
        {
            "basis": rec.basis.value,
            "basis_id": rec.basis_id,
            "basis_amount": rec.amount,
            "state": rec.state.value,
            "counted": rec.counted,
            "documents": [basis, *(_doc_out(d) for d in rec.evidence)],
            "candidates": [_doc_out(d) for d in rec.candidates],
            "amount_conflict": bool(rec.amount_conflicts),
            "confirmation_required": confirmation,
            "notes": list(rec.notes),
        },
    )


def _term_source(snap: Snapshot, ag: Agreement) -> dict[str, Any]:
    """Where an agreement's payment_term_days came from (fact id + source span)."""
    for fid in ag.facts:
        f = snap.fact_index.get(fid)
        if f is not None and (
            f.kind.endswith("payment_term_days") or fid.endswith(":payment_term_days")
        ):
            span: SourceSpan | None = f.span
            return {"agreement_id": ag.id, "fact_id": fid, "span": span}
    return {"agreement_id": ag.id, "fact_id": None, "span": None}


@dataclass(frozen=True)
class _ContractOut:
    computation: Computation
    assumptions: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()


def _contract_block(
    rec: ReceivableDocument,
    trade_type: TradeType | None,
    appl: list[Agreement],
    statutory: DueResult | Insufficient | None,
    snap: Snapshot,
) -> _ContractOut | None:
    """Contractual due date (약정 기한) from applicable agreements' payment_term_days.

    Shown next to the statutory due date; interest is never computed for it."""
    with_term = [a for a in appl if a.payment_term_days is not None]
    if not with_term:
        return None
    terms = sorted({int(a.payment_term_days) for a in with_term if a.payment_term_days is not None})
    sources = [_term_source(snap, a) for a in with_term]
    if len(terms) > 1:
        return _ContractOut(
            Computation(
                "contractual_due",
                None,
                {"payment_term_days": terms},
                {"conflict": True, "values": terms, "source": sources, "interest": None},
            ),
            (f"적용 가능한 약정의 지급기한 일수가 서로 다름: {', '.join(map(str, terms))}일",),
            (UNRESOLVED_TERM_CONFLICT,),
        )
    base = getattr(rec, BASE_FIELD[trade_type]) if trade_type is not None else None
    res = compute_contractual_due(terms[0], trade_type, base, calendar=snap.config.calendar)
    inputs = {"payment_term_days": terms[0], "trade_type": trade_type, "base_date": base}
    if isinstance(res, Insufficient):
        return _ContractOut(
            Computation(
                "contractual_due",
                None,
                inputs,
                {
                    "insufficient": True,
                    "missing": res.missing,
                    "source": sources,
                    "interest": None,
                },
            )
        )
    assert isinstance(res, ContractualDue)
    comparison = []
    if isinstance(statutory, DueResult):
        comparison = [
            {
                "label": v.label,
                "statutory_due": v.due_date,
                "difference_days": difference_days(res.due_date, v.due_date),
            }
            for v in statutory.variants
        ]
    return _ContractOut(
        Computation(
            "contractual_due",
            CONTRACT_TERM_ID,
            inputs,
            {
                "insufficient": False,
                "conflict": False,
                "term_days": res.term_days,
                "base_field": res.base_field,
                "base_date": res.base_date,
                "due_date": res.due_date,
                "is_business_day": res.is_business_day,
                "interest": None,  # never computed: the statutory rate is not applied
                "interest_note": NO_INTEREST_NOTE,
                "statutory_comparison": comparison,
                "source": sources,
                "unresolved": list(res.unresolved),
            },
        ),
        res.assumptions,
        res.unresolved,
    )


def _pending_status(rec: Receivable) -> ReconcileStatus:
    """A possible duplicate is AMBIGUOUS (two readings of the documents); an invoice whose
    direction is unknown lacks information -> INSUFFICIENT_EVIDENCE."""
    if rec.pending is PendingKind.INVOICE_DIRECTION:
        return ReconcileStatus.INSUFFICIENT_EVIDENCE
    return ReconcileStatus.AMBIGUOUS


def _pending_decision_parts(
    rec: Receivable, doc: ReceivableDocument, txn_ids: tuple[str, ...]
) -> tuple[list[Computation], list[str]]:
    """Computations + assumptions of a document that is not (yet) a counted receivable:
    its link to a sale, its duplicate status or its direction is not confirmed."""
    zero = Money.zero(doc.amount.currency)
    label = DOC_LABEL[rec.basis.value]
    what = {
        PendingKind.EVIDENCE_LINK: "같은 거래처의 정산 행과 같은 거래인지 확인 전",
        PendingKind.DUPLICATE_LINE: "다른 문서의 같은 참조번호 정산 행과 같은 행인지 확인 전",
        PendingKind.INVOICE_DIRECTION: (
            "매출·매입 구분 또는 거래처를 알 수 없어 받을 돈인지 확인 전"
        ),
    }[rec.pending]
    note = f"{label} [{doc.id}] {doc.amount}: {what} — 미수 합계와 입금 배분에서 제외"
    recon = Computation(
        "recon",
        None,
        {"amount": doc.amount, "payments_in_group": txn_ids},
        {
            "status": _pending_status(rec),
            "allocated": zero,
            "fee_difference": zero,
            "open": zero,  # not counted: see evidence.counted / confirmation_required
            "candidates": (),
            "reasons": (note,),
            "counted": False,
        },
    )
    return [recon, _evidence_computation(rec)], [note, *rec.notes]


def analyze_group(
    snap: Snapshot, group: str
) -> tuple[GroupOutcome, list[tuple[Decision, list[tuple[str, str]], dict[QueryScope, str]]]]:
    cfg = snap.config
    receivables = snap.receivable_model_groups.get(group, ())
    txns: tuple[BankTxn, ...] = snap.txn_groups.get(group, ())
    agreements = snap.agreement_groups.get(group, ())
    group_links = snap.link_groups.get(group, ())
    # only established receivables take part in reconciliation (no double counting)
    items = [_item_for(r, snap) for r in receivables if r.counted]
    payments = [payment_from_txn(t, counterparty=group) for t in txns]
    recon = reconcile(items, payments, cfg.recon)
    pay_dates = {t.id: t.booked_date for t in txns}
    txn_by_id = {t.id: t for t in txns}
    scopes = {
        QueryScope("receivables", group): snap.evaluate_scope(QueryScope("receivables", group)),
        QueryScope("bank_txns", group): snap.evaluate_scope(QueryScope("bank_txns", group)),
        QueryScope("agreements", group): snap.evaluate_scope(QueryScope("agreements", group)),
        QueryScope("config"): snap.evaluate_scope(QueryScope("config")),
    }
    txn_ids = tuple(sorted(txn_by_id))
    out: list[tuple[Decision, list[tuple[str, str]], dict[QueryScope, str]]] = []
    for model in receivables:
        rec = snap.documents[model.basis_id]  # the basis document (amount, dates, trade type)
        did = decision_id_for(model.id)
        assumptions: list[str] = []
        unresolved: list[str] = []
        required: list[str] = []
        missing: list[str] = []
        status_override: ReconcileStatus | None = None
        rule_versions: list[str] = []
        uses_as_of = False
        # confirmations that decided how this receivable's documents are linked
        evidence_ids = {model.basis_id, *(d.id for d in model.evidence)}
        used_links = [lk for lk in group_links if lk.invoice_id in evidence_ids]
        appl: list[Agreement] = []
        ir: ItemResult | None = None

        if not model.counted:
            # not a counted receivable (uncertain link / possible duplicate / unknown
            # direction): ask instead of counting the document twice or as owed money
            comps, notes = _pending_decision_parts(model, rec, txn_ids)
            assumptions.extend(notes)
            status = _pending_status(model)
            unresolved.append(model.pending.value)
            required.extend(PENDING_DOCS[model.pending])
            missing.extend(model.missing)
        else:
            ir = recon.items[model.id]
            appl = _applicable(agreements, _ref_date(rec))
            if not appl:
                assumptions.append(f"적용 약정 없음(거래처 '{group}' 약정 조회 결과 0건)")
            ag_trade, c1 = _one(a.trade_type for a in appl)
            ag_roll, c2 = _one(a.rollover for a in appl)
            ag_round, c3 = _one(a.rounding for a in appl)
            ag_month, c4 = _one(a.monthly_settlement for a in appl)
            if c1 or c2 or c3 or c4:
                status_override = ReconcileStatus.CONFLICT
                unresolved.append("agreement_conflict")
                required.extend(CONFLICT_DOCS)
                assumptions.append("적용 가능한 약정들의 조건이 서로 다름")
            trade_type = rec.trade_type
            if trade_type is not None and ag_trade is not None and ag_trade != trade_type:
                status_override = ReconcileStatus.CONFLICT
                unresolved.append("trade_type_conflict")
                assumptions.append(f"거래 형태 불일치: 문서 {trade_type.value}, 약정 {ag_trade}")
            trade_type = trade_type or (TradeType(ag_trade) if ag_trade else None)
            rollover = ag_roll if ag_roll is not None else cfg.rollover
            rounding = ag_round if ag_round is not None else cfg.rounding

            conflicts = model.amount_conflicts
            if conflicts:
                # linked documents of one sale disagree on the amount: show both, decide nothing
                status_override = ReconcileStatus.CONFLICT
                unresolved.append(UNRESOLVED_AMOUNT)
                required.extend(AMOUNT_CONFLICT_DOCS)
                label = DOC_LABEL[model.basis.value]
                for d in conflicts:
                    assumptions.append(
                        f"연결된 문서의 금액이 다름: {label} [{model.basis_id}] {model.amount}"
                        f" / {DOC_LABEL[d.entity]} [{d.id}] {d.amount}"
                    )
            if model.evidence:
                assumptions.append(
                    f"금액·날짜 기준 문서: {DOC_LABEL[model.basis.value]} [{model.basis_id}];"
                    " 연결된 문서는 금액을 더하지 않는 보조 근거"
                )

            comps = [
                Computation(
                    "recon",
                    None,
                    {"amount": rec.amount, "payments_in_group": txn_ids},
                    {
                        "status": ir.status,
                        "allocated": ir.allocated,
                        "fee_difference": ir.fee_difference,
                        "open": ir.open,
                        "candidates": ir.candidates,
                        "reasons": ir.reasons,
                    },
                ),
                _evidence_computation(model),
            ]
            due_needed = rec.amount.is_positive and not (
                isinstance(rec, SettlementLine) and rec.line_kind is not LineKind.SALE
            )
            insufficient = False
            statutory: DueResult | Insufficient | None = None
            if due_needed and status_override is None:
                due = _due_block(
                    rec,
                    ir,
                    trade_type,
                    rollover,
                    rounding,
                    ag_month,
                    pay_dates,
                    snap,
                    withhold_delay=ir.status is ReconcileStatus.AMBIGUOUS,
                )
                comps.append(due.computation)
                head = statutory = due.head
                uses_as_of = due.uses_as_of
                if isinstance(head, Insufficient):
                    insufficient = True
                    missing.extend(head.missing)
                    required.extend(head.required_documents)
                    assumptions.extend(head.notes)
                else:
                    rule_versions.append(head.rule_version)
                    if head.interest_rule_version:
                        rule_versions.append(head.interest_rule_version)
                    unresolved.extend(head.unresolved)
                    # the head call has no end date; per-tranche end dates replace that note
                    assumptions.extend(a for a in head.assumptions if a != END_UNSET_NOTE)
                    assumptions.extend(due.tranche_notes)
                contract = _contract_block(rec, trade_type, appl, statutory, snap)
                if contract is not None:
                    comps.append(contract.computation)
                    assumptions.extend(contract.assumptions)
                    unresolved.extend(contract.unresolved)
            elif not due_needed:
                assumptions.append("공제·반품 등 차감 항목: 지급기한 계산 대상 아님")

            if status_override is not None:
                status = status_override
            elif ir.status in (ReconcileStatus.CONFLICT, ReconcileStatus.AMBIGUOUS):
                status = ir.status
            elif insufficient:
                status = ReconcileStatus.INSUFFICIENT_EVIDENCE
            else:
                status = ir.status
            if ir.status is ReconcileStatus.AMBIGUOUS:
                unresolved.append("allocation")
                required.extend(AMBIGUOUS_DOCS)

        # provenance edges & facts
        edges: list[tuple[str, str]] = []
        facts_used: list[str] = []
        rec_node = node_rec(_entity(rec), rec.id)
        comp_nodes = [node_comp(did, c.name) for c in comps]

        _link(snap, edges, facts_used, rec_node, rec.facts, comp_nodes)
        for d in model.evidence:  # corroborating documents are evidence of this decision
            doc = snap.documents[d.id]
            _link(snap, edges, facts_used, node_rec(d.entity, d.id), doc.facts, comp_nodes)
        for d in model.candidates:  # edges only: a candidate is not used as evidence
            edges.append((node_rec(d.entity, d.id), node_comp(did, "evidence")))
        for lk in used_links:
            _link(snap, edges, facts_used, node_rec("evidence_link", lk.id), lk.facts, comp_nodes)
        for a in ir.allocations if ir is not None else ():
            t = txn_by_id[a.source_id]
            _link(
                snap,
                edges,
                facts_used,
                node_rec("bank_txn", t.id),
                t.facts,
                [node_comp(did, "recon")],
            )
        for ag in appl:
            _link(snap, edges, facts_used, node_rec("agreement", ag.id), ag.facts, comp_nodes)
        for cn in comp_nodes:
            edges.append((cn, node_dec(did)))
        facts_tuple = tuple(dict.fromkeys(facts_used))
        dec_scopes = dict(scopes)
        if uses_as_of:
            dec_scopes[QueryScope("as_of")] = snap.evaluate_scope(QueryScope("as_of"))
        inputs_hash = content_hash(
            {
                "scopes": {str(s): h for s, h in dec_scopes.items()},
                "facts": {
                    fid: (content_hash(snap.fact_index[fid]) if fid in snap.fact_index else None)
                    for fid in facts_tuple
                },
            }
        )
        dec = Decision.build(
            id=did,
            tenant_id=snap.tenant_id,
            subject_id=model.id,
            status=status,
            snapshot_hash=snap.snapshot_hash,
            facts_used=facts_tuple,
            rule_versions=tuple(rule_versions),
            computations=tuple(comps),
            unresolved=tuple(dict.fromkeys(unresolved)),
            required_documents=tuple(dict.fromkeys(required)),
            allocations=tuple(ir.allocations) if ir is not None else (),
            assumptions=tuple(dict.fromkeys(assumptions)),
            missing=tuple(dict.fromkeys(missing)),
            inputs_hash=inputs_hash,
        )
        out.append((dec, edges, dec_scopes))
    outcome = GroupOutcome(group, tuple(d.id for d, _, _ in out), dict(recon.payments))
    return outcome, out


def full_recompute(snap: Snapshot) -> AnalysisResult:
    graph = DependencyGraph()
    decisions: dict[str, Decision] = {}
    groups: dict[str, GroupOutcome] = {}
    for g in snap.receivable_groups:
        outcome, rows = analyze_group(snap, g)
        groups[g] = outcome
        for dec, edges, scopes in rows:
            decisions[dec.id] = dec
            graph.add_decision(dec.id, edges, scopes)
    return AnalysisResult(
        snap.tenant_id, snap.snapshot_hash, decisions, groups, snap.unattributed_txns, graph
    )


def incremental_recompute(
    prev: AnalysisResult, snap: Snapshot, changes: Iterable[Change]
) -> tuple[AnalysisResult, RecomputePlan, frozenset[str]]:
    """Recompute only affected groups. Returns (result, plan, recomputed groups)."""
    p = plan_invalidation(prev.graph, list(changes), snap, existing_decisions=prev.decisions.keys())
    if p.fallback_full:
        return full_recompute(snap), p, frozenset(snap.receivable_groups)
    prev_group = prev.decision_group()
    todo: set[str] = set()
    for d in p.affected | p.removed:
        if d in prev_group:
            todo.add(prev_group[d])
        subject = d.removeprefix("dec:")
        g = snap.group_of(subject)
        if g is not None:
            todo.add(g)
    graph = prev.graph.copy()
    decisions = dict(prev.decisions)
    groups = dict(prev.groups)
    for g in todo:
        old = groups.pop(g, None)
        if old is not None:
            for d in old.decision_ids:
                decisions.pop(d, None)
                graph.remove_decision(d)
    for g in sorted(todo):
        if g not in snap.receivable_groups:
            continue
        outcome, rows = analyze_group(snap, g)
        groups[g] = outcome
        for dec, edges, scopes in rows:
            decisions[dec.id] = dec
            graph.add_decision(dec.id, edges, scopes)
    # unaffected decisions are rebased on the new snapshot (content unchanged)
    for d, dec in list(decisions.items()):
        if dec.snapshot_hash != snap.snapshot_hash:
            decisions[d] = dataclasses.replace(dec, snapshot_hash=snap.snapshot_hash)
    result = AnalysisResult(
        snap.tenant_id,
        snap.snapshot_hash,
        dict(sorted(decisions.items())),
        dict(sorted(groups.items())),
        snap.unattributed_txns,
        graph,
    )
    return result, p, frozenset(todo)


def total_allocated(allocs: Iterable[Allocation], currency: str = "KRW") -> Money:
    return sum_money([a.amount for a in allocs], currency)
