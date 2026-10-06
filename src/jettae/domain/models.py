"""Core domain records (pure, frozen dataclasses).

Field order of the public constructors follows docs/ARCHITECTURE.md "Core contracts".
Trailing fields have defaults so adapters may construct records positionally or by keyword.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from jettae.domain.errors import DomainValidationError
from jettae.domain.hashing import content_hash
from jettae.domain.money import Money, RoundingMode
from jettae.domain.status import (
    ChangeKind,
    DocKind,
    DocumentStatus,
    LineKind,
    ReconcileStatus,
    TradeType,
)

EMPTY: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DocumentVersion:
    id: str
    tenant_id: str
    document_id: str
    version: int
    content_hash: str
    filename: str
    media_type: str
    kind: DocKind
    created_at: datetime
    size: int = 0
    supersedes: str | None = None
    status: DocumentStatus = DocumentStatus.REGISTERED
    storage_key: str | None = None


@dataclass(frozen=True)
class SourceSpan:
    """Where a fact came from: document version + locator + verbatim excerpt.

    ``locator`` examples: ``{"sheet": "Sheet1", "row": 12, "col": "D"}`` or
    ``{"page": 3, "char_start": 10, "char_end": 42}``.
    """

    doc_version_id: str
    locator: Mapping[str, Any]
    excerpt: str


@dataclass(frozen=True)
class Fact:
    id: str
    tenant_id: str
    kind: str
    value: Any
    span: SourceSpan | None
    extractor: str
    observed_at: datetime  # known time (system, UTC)
    valid_from: date | None = None  # valid time
    valid_to: date | None = None
    supersedes: str | None = None
    subject_id: str | None = None  # ledger record this fact describes


# ---------------------------------------------------------------- ledger records
@dataclass(frozen=True)
class Invoice:
    """A receivable document (e.g. 세금계산서). ``issue_date`` is NOT a statutory base date."""

    id: str
    tenant_id: str
    counterparty: str
    amount: Money
    issue_date: date | None = None
    reference: str | None = None
    trade_type: TradeType | None = None
    goods_received_date: date | None = None  # 상품수령일 / 목적물 수령일
    sales_close_date: date | None = None  # 월 판매마감일
    facts: tuple[str, ...] = ()
    missing: frozenset[str] = EMPTY


@dataclass(frozen=True)
class SettlementLine:
    """A line of a retailer settlement statement (정산서). Negative amounts = deductions."""

    id: str
    tenant_id: str
    counterparty: str
    amount: Money
    line_kind: LineKind = LineKind.SALE
    period_start: date | None = None
    period_end: date | None = None
    sales_close_date: date | None = None
    goods_received_date: date | None = None
    settlement_ref: str | None = None
    reference: str | None = None
    trade_type: TradeType | None = None
    facts: tuple[str, ...] = ()
    missing: frozenset[str] = EMPTY


@dataclass(frozen=True)
class BankTxn:
    """A bank transaction. Positive = money received; negative = money paid out / reversal."""

    id: str
    tenant_id: str
    booked_date: date
    amount: Money
    counterparty: str | None = None
    memo: str = ""
    reference: str | None = None
    account: str | None = None
    facts: tuple[str, ...] = ()
    missing: frozenset[str] = EMPTY


@dataclass(frozen=True)
class Agreement:
    """Agreed trade conditions (약정). ``None`` = condition not confirmed by a document."""

    id: str
    tenant_id: str
    counterparty: str
    trade_type: TradeType | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    payment_term_days: int | None = None
    rollover: bool | None = None
    rounding: RoundingMode | None = None
    monthly_settlement: bool | None = None
    facts: tuple[str, ...] = ()
    missing: frozenset[str] = EMPTY


LedgerRecord = Invoice | SettlementLine | BankTxn | Agreement

# A document row on the "money owed to us" side. One sale may be evidenced by several of
# these (a settlement line AND a tax invoice); see :class:`Receivable` for the economic item.
ReceivableDocument = Invoice | SettlementLine


# ---------------------------------------------------------------- receivables
class ReceivableBasis(StrEnum):
    """Which document an economic receivable's amount and dates are taken from.

    Settlement lines are the basis whenever the sale has one: the retailer's statement is
    what the payment settles. A tax invoice is the basis only when no settlement line is
    linked to it (and none in the same counterparty could be the same sale, or the user
    confirmed it is a separate sale)."""

    SETTLEMENT_LINE = "settlement_line"
    INVOICE = "invoice"


class EvidenceRole(StrEnum):
    BASIS = "basis"  # amount / dates / trade type come from this document
    CORROBORATING = "corroborating"  # same sale, explicitly linked; never adds an amount
    CANDIDATE = "candidate"  # might be the same sale; link needs confirmation


class LinkMethod(StrEnum):
    """How a corroborating document was linked. Equal amount or date is NEVER a method."""

    REFERENCE = "reference"  # same counterparty + same explicit reference number
    USER = "user_confirmation"  # an :class:`EvidenceLink` record


class LinkRelation(StrEnum):
    SAME_SALE = "same_sale"
    SEPARATE_SALE = "separate_sale"


class ReceivableState(StrEnum):
    ESTABLISHED = "established"  # counted in receivable totals and reconciled with payments
    NEEDS_CONFIRMATION = "needs_confirmation"  # may duplicate another receivable; not counted


class PendingKind(StrEnum):
    """Why a receivable is in state ``NEEDS_CONFIRMATION`` (the decision's unresolved item).

    ``EVIDENCE_LINK``: a tax invoice that may be the same sale as a settlement line.
    ``DUPLICATE_LINE``: a settlement line whose counterparty and reference equal a line of
    another (earlier) document - e.g. the same statement uploaded again as a re-issued file.
    ``INVOICE_DIRECTION``: a tax invoice whose direction (sales/purchase) or counterparty is
    unknown, so it is not known to be money owed to the user at all."""

    EVIDENCE_LINK = "evidence_link"
    DUPLICATE_LINE = "duplicate_line"
    INVOICE_DIRECTION = "invoice_direction"


@dataclass(frozen=True)
class EvidenceLink:
    """A user's confirmation of how a document relates to the settlement lines of the same
    counterparty.

    ``invoice_id`` is the confirmed document's id. For historical reasons (stored records)
    the field keeps its name, but since ``document_entity`` exists it may also name a
    settlement line flagged as a possible duplicate of another document's line
    (``document_entity="settlement_line"``).

    ``SAME_SALE`` + ``settlement_line_id``: the document is corroborating evidence of that
    line (the line stays the basis; no amount is added). ``SEPARATE_SALE``: the document is
    its own receivable even though settlement lines of the same counterparty (or with the
    same reference) exist. The record is input evidence (it is part of the snapshot and of
    the ``receivables:<counterparty>`` query scope), so adding or removing it invalidates
    the affected decisions like any other document.
    """

    id: str
    tenant_id: str
    invoice_id: str
    relation: LinkRelation
    settlement_line_id: str | None = None
    confirmed_by: str = ""
    note: str = ""
    facts: tuple[str, ...] = ()
    document_entity: str = "invoice"  # "invoice" | "settlement_line"

    @property
    def document_id(self) -> str:
        return self.invoice_id

    def __post_init__(self) -> None:
        rel = LinkRelation(self.relation)
        object.__setattr__(self, "relation", rel)
        if self.document_entity not in ("invoice", "settlement_line"):
            raise DomainValidationError(f"unknown document_entity {self.document_entity!r}")
        if self.settlement_line_id is not None and self.settlement_line_id == self.invoice_id:
            raise DomainValidationError("a document cannot be linked to itself")
        if rel is LinkRelation.SAME_SALE and not self.settlement_line_id:
            raise DomainValidationError("same_sale link requires settlement_line_id")
        if rel is LinkRelation.SEPARATE_SALE and self.settlement_line_id:
            raise DomainValidationError("separate_sale link must not name a settlement line")


@dataclass(frozen=True)
class DocumentRef:
    """One document's part in an economic receivable."""

    entity: str  # "invoice" | "settlement_line"
    id: str
    amount: Money
    role: EvidenceRole
    method: LinkMethod | None = None  # None for the basis and for unconfirmed candidates
    reason: str = ""


@dataclass(frozen=True)
class Receivable:
    """An economic receivable (one sale = one amount owed), as opposed to a document row.

    ``id`` equals ``basis_id`` so that decision ids (``dec:<basis document id>``) stay stable
    for receivables that are evidenced by a single document. ``amount`` is the basis
    document's amount; corroborating documents never add to it. A receivable in state
    ``NEEDS_CONFIRMATION`` is a document that may be another document's duplicate, or that
    may not be money owed to the user at all (``pending``): it gets its own decision (with
    the ``pending`` kind as unresolved item) but is not counted in totals and no payment is
    allocated to it until the user confirms / supplies the missing information.
    """

    id: str
    tenant_id: str
    counterparty: str  # normalised counterparty key (the reconciliation group)
    amount: Money
    basis: ReceivableBasis
    basis_id: str
    state: ReceivableState = ReceivableState.ESTABLISHED
    evidence: tuple[DocumentRef, ...] = ()  # corroborating documents (basis excluded)
    candidates: tuple[DocumentRef, ...] = ()  # documents it might duplicate
    notes: tuple[str, ...] = ()
    pending: PendingKind = PendingKind.EVIDENCE_LINK  # meaningful only when not counted
    missing: tuple[str, ...] = ()  # basis-document fields whose absence keeps it pending

    @property
    def counted(self) -> bool:
        return self.state is ReceivableState.ESTABLISHED

    @property
    def amount_conflicts(self) -> tuple[DocumentRef, ...]:
        """Linked documents whose amount differs from the basis (-> CONFLICT)."""
        return tuple(d for d in self.evidence if d.amount != self.amount)


# The same item seen from the payer's side (what the retailer owes). Alias, not a new type.
Obligation = Receivable


@dataclass(frozen=True)
class Allocation:
    """``amount`` of payment ``source_id`` applied to receivable ``target_id``."""

    source_id: str
    target_id: str
    amount: Money
    evidence: tuple[str, ...] = ()


# ---------------------------------------------------------------- decisions
@dataclass(frozen=True)
class Computation:
    name: str
    rule_version: str | None
    inputs: Mapping[str, Any]
    outputs: Mapping[str, Any]


@dataclass(frozen=True)
class Decision:
    id: str
    tenant_id: str
    subject_id: str
    status: ReconcileStatus
    facts_used: tuple[str, ...]
    rule_versions: tuple[str, ...]
    computations: tuple[Computation, ...]
    unresolved: tuple[str, ...]
    required_documents: tuple[str, ...]
    snapshot_hash: str
    result_hash: str
    allocations: tuple[Allocation, ...] = ()
    assumptions: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    inputs_hash: str = ""  # hash of this decision's own dependencies (facts + scope results)

    @staticmethod
    def compute_result_hash(
        *,
        subject_id: str,
        status: ReconcileStatus,
        facts_used: tuple[str, ...],
        rule_versions: tuple[str, ...],
        computations: tuple[Computation, ...],
        unresolved: tuple[str, ...],
        required_documents: tuple[str, ...],
        allocations: tuple[Allocation, ...],
        assumptions: tuple[str, ...],
        missing: tuple[str, ...],
        inputs_hash: str,
    ) -> str:
        return content_hash(
            {
                "subject_id": subject_id,
                "status": status,
                "facts_used": facts_used,
                "rule_versions": rule_versions,
                "computations": computations,
                "unresolved": unresolved,
                "required_documents": required_documents,
                "allocations": allocations,
                "assumptions": assumptions,
                "missing": missing,
                "inputs_hash": inputs_hash,
            }
        )

    @classmethod
    def build(
        cls,
        *,
        id: str,
        tenant_id: str,
        subject_id: str,
        status: ReconcileStatus,
        snapshot_hash: str,
        facts_used: tuple[str, ...] = (),
        rule_versions: tuple[str, ...] = (),
        computations: tuple[Computation, ...] = (),
        unresolved: tuple[str, ...] = (),
        required_documents: tuple[str, ...] = (),
        allocations: tuple[Allocation, ...] = (),
        assumptions: tuple[str, ...] = (),
        missing: tuple[str, ...] = (),
        inputs_hash: str = "",
    ) -> Decision:
        rh = cls.compute_result_hash(
            subject_id=subject_id,
            status=status,
            facts_used=facts_used,
            rule_versions=rule_versions,
            computations=computations,
            unresolved=unresolved,
            required_documents=required_documents,
            allocations=allocations,
            assumptions=assumptions,
            missing=missing,
            inputs_hash=inputs_hash,
        )
        return cls(
            id=id,
            tenant_id=tenant_id,
            subject_id=subject_id,
            status=status,
            facts_used=facts_used,
            rule_versions=rule_versions,
            computations=computations,
            unresolved=unresolved,
            required_documents=required_documents,
            snapshot_hash=snapshot_hash,
            result_hash=rh,
            allocations=allocations,
            assumptions=assumptions,
            missing=missing,
            inputs_hash=inputs_hash,
        )

    def computation(self, name: str) -> Computation | None:
        for c in self.computations:
            if c.name == name:
                return c
        return None


@dataclass(frozen=True)
class Approval:
    """An approval bound to a result hash and input snapshot. Never mutated; validity is
    computed by comparing ``result_hash`` with the decision's current result hash."""

    decision_id: str
    result_hash: str
    snapshot_hash: str
    approved_by: str
    approved_at: datetime
    tenant_id: str
    id: str = ""


@dataclass(frozen=True)
class Change:
    """A change to the evidence base, used by the invalidation planner.

    ``entity``: one of ``invoice``, ``settlement_line``, ``bank_txn``, ``agreement``,
    ``evidence_link``, ``fact``, ``doc_version``, ``config``. Anything else is treated as
    untracked (the planner falls back to a full recompute).
    """

    kind: ChangeKind
    entity: str
    entity_id: str
    record: Any = None


@dataclass(frozen=True)
class RecomputePlan:
    affected: frozenset[str]
    reasons: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    fallback_full: bool = False
    removed: frozenset[str] = EMPTY
    notes: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        parts = list(self.notes)
        for did in sorted(self.reasons):
            parts.append(f"{did}: {'; '.join(self.reasons[did])}")
        return " | ".join(parts)
