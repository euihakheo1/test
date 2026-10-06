"""Economic receivables from document rows (pure).

Document rows are not receivables. A sale can be evidenced by several documents: a line of
the retailer's settlement statement (정산서) and a tax invoice (세금계산서) for the same goods,
or the same statement uploaded twice (a re-issued or re-exported file whose bytes differ, so
file-hash de-duplication does not catch it). Counting each document row as a receivable
double-counts the sale. This module turns the document rows of ONE counterparty group into
:class:`~jettae.domain.models.Receivable` items:

1. Every settlement line is a receivable with ``basis = settlement_line`` (deduction / return
   / fee lines included; they are credits of the statement and are never linked to invoices)
   - except a *duplicate line* (rule 5).
2. A tax invoice is linked to a settlement SALE line as *corroborating* evidence only by
   - an explicit reference: same counterparty group AND the invoice's normalised reference
     equals the reference of exactly one SALE line of that group; or
   - a user confirmation (:class:`~jettae.domain.models.EvidenceLink`, ``same_sale``).
   Equal amount or equal date is never a link (two deliveries of the same price are two
   sales). A linked invoice adds no amount; if its amount differs from the line, the
   receivable reports an amount conflict (the engine marks the decision CONFLICT).
3. An invoice that is not linked becomes a receivable itself (``basis = invoice``) only when
   it cannot be one of the group's settlement SALE lines: the group has none, or the user
   confirmed ``separate_sale``.
4. Otherwise the link is uncertain (reference matches several lines, or the group has SALE
   lines but no reference matches): the invoice becomes a receivable in state
   ``needs_confirmation`` (``pending = evidence_link``). It gets its own AMBIGUOUS decision
   with a confirmation item, is not counted in totals and receives no payment allocation
   until the user confirms.
5. **Duplicate lines.** A SALE line is a possible duplicate when an *earlier* SALE line of
   the same group, read from a *different* document version, has the same normalised
   reference (or, for lines without a reference, the same settlement number and amount).
   "Earlier" = the line's provenance (``origins``: the version registration time of the
   facts it was read from, then the version id). The earliest line stays the basis; each
   later one is ``needs_confirmation`` (``pending = duplicate_line``, candidates = the
   earlier lines) until the user confirms ``separate_sale`` (counted) or ``same_sale``
   (it becomes corroborating evidence of the named line, adding no amount). Lines of the
   *same* document version are never duplicates of each other (a statement may list a PO
   twice for two deliveries). Lines without provenance (no source spans) are not checked.
6. **Invoices of unknown direction.** A tax invoice whose direction (sales/purchase) is
   unknown or whose counterparty is empty is not known to be money owed to the user (it may
   be a purchase invoice), so it is never its own receivable: it is ``needs_confirmation``
   with ``pending = invoice_direction`` and ``missing`` naming the unknown fields, until the
   document is re-read with the direction (mapping options ``self_brn`` / ``direction``).

The result depends only on the set of documents, their provenance and the confirmations of
the group (inputs are sorted by id), so upload order never changes it, and adding an invoice
that explicitly references an existing settlement line never increases the counted amount.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from jettae.domain.models import (
    DocumentRef,
    EvidenceLink,
    EvidenceRole,
    Invoice,
    LinkMethod,
    LinkRelation,
    PendingKind,
    Receivable,
    ReceivableBasis,
    ReceivableDocument,
    ReceivableState,
    SettlementLine,
)
from jettae.domain.status import LineKind
from jettae.recon.candidates import normalize_counterparty, normalize_ref

INVOICE = "invoice"
SETTLEMENT_LINE = "settlement_line"

REASON_MULTI_REF = "참조번호가 같은 거래처의 판매 정산 행 여러 건과 일치"
REASON_NO_REF = (
    "같은 거래처에 판매 정산 행이 있으나 참조번호로 연결되지 않음 (금액·날짜만으로는 연결하지 않음)"
)
REASON_CONFLICTING_CONFIRMATIONS = "같은 문서에 대한 사용자 확인 기록이 서로 다름"
REASON_DUPLICATE_LINE = (
    "다른 문서(먼저 올린 문서)에 같은 거래처·같은 참조번호의 판매 정산 행이 있음 — 같은 정산서를 "
    "다시 올린 것인지 확인 전"
)
REASON_DIRECTION = (
    "매출·매입 구분 또는 거래처(공급받는자)를 알 수 없는 세금계산서 — 받을 돈인지 확인 전"
)

# Provenance of a document row: (registration time of the version it was read from, that
# version's id). Rows without source spans have no entry.
Origin = tuple[datetime, str]


def entity_of_document(doc: ReceivableDocument) -> str:
    return INVOICE if isinstance(doc, Invoice) else SETTLEMENT_LINE


def link_group_of(link: EvidenceLink, documents: dict[str, ReceivableDocument]) -> str | None:
    """Counterparty group a confirmation belongs to: the group of the confirmed document
    (None if that document is not in the snapshot or is not of the declared entity; the
    confirmation then has no effect)."""
    doc = documents.get(link.invoice_id)
    expected = Invoice if link.document_entity == INVOICE else SettlementLine
    if not isinstance(doc, expected):
        return None
    return normalize_counterparty(doc.counterparty)


def _ref(
    doc: ReceivableDocument, role: EvidenceRole, method: LinkMethod | None, why: str
) -> DocumentRef:
    return DocumentRef(entity_of_document(doc), doc.id, doc.amount, role, method, why)


def duplicate_key(line: SettlementLine) -> tuple[str, ...] | None:
    """Identity of a settlement SALE line across documents (rule 5): the normalised
    reference, else the settlement number plus amount; None = not comparable."""
    ref = normalize_ref(line.reference)
    if ref:
        return ("ref", ref)
    sref = normalize_ref(line.settlement_ref)
    if sref:
        return ("settlement_ref", sref, line.amount.currency, str(line.amount.amount))
    return None


def duplicate_candidates(
    sale_lines: Sequence[SettlementLine], origins: Mapping[str, Origin]
) -> dict[str, tuple[SettlementLine, ...]]:
    """Line id -> earlier lines of *other* document versions with the same duplicate key."""
    keyed: dict[tuple[str, ...], list[SettlementLine]] = {}
    for ln in sale_lines:
        k = duplicate_key(ln)
        if k is not None and ln.id in origins:
            keyed.setdefault(k, []).append(ln)
    out: dict[str, tuple[SettlementLine, ...]] = {}
    for group in keyed.values():
        ordered = sorted(group, key=lambda ln: (origins[ln.id], ln.id))
        for i, ln in enumerate(ordered):
            earlier = tuple(e for e in ordered[:i] if origins[e.id][1] != origins[ln.id][1])
            if earlier:
                out[ln.id] = earlier
    return out


def _direction_unknown(inv: Invoice) -> tuple[str, ...]:
    missing = set()
    if "direction" in inv.missing:
        missing.add("direction")
    if not normalize_counterparty(inv.counterparty):
        missing.add("counterparty")
    return tuple(sorted(missing))


def build_group_receivables(
    tenant_id: str,
    group: str,
    documents: Sequence[ReceivableDocument],
    links: Iterable[EvidenceLink] = (),
    origins: Mapping[str, Origin] | None = None,
) -> tuple[Receivable, ...]:
    """Receivables of one counterparty group, sorted by id (see module docstring)."""
    docs = sorted(documents, key=lambda d: d.id)
    lines = [d for d in docs if isinstance(d, SettlementLine)]
    invoices = [d for d in docs if isinstance(d, Invoice)]
    all_sale_lines = [ln for ln in lines if ln.line_kind is LineKind.SALE]
    links_by_doc: dict[str, list[EvidenceLink]] = {}
    for lk in sorted(links, key=lambda x: x.id):
        links_by_doc.setdefault(lk.invoice_id, []).append(lk)

    attached: dict[str, list[DocumentRef]] = {}
    pending_on_line: dict[str, list[DocumentRef]] = {}
    out: list[Receivable] = []

    def pending(
        doc: ReceivableDocument,
        cands: tuple[DocumentRef, ...],
        why: str,
        kind: PendingKind = PendingKind.EVIDENCE_LINK,
        missing: tuple[str, ...] = (),
    ) -> None:
        out.append(
            Receivable(
                id=doc.id,
                tenant_id=tenant_id,
                counterparty=group,
                amount=doc.amount,
                basis=ReceivableBasis(entity_of_document(doc)),
                basis_id=doc.id,
                state=ReceivableState.NEEDS_CONFIRMATION,
                candidates=cands,
                notes=(why,),
                pending=kind,
                missing=missing,
            )
        )
        for c in cands:
            pending_on_line.setdefault(c.id, []).append(
                _ref(doc, EvidenceRole.CANDIDATE, None, why)
            )

    def own(doc: ReceivableDocument, notes: tuple[str, ...] = ()) -> None:
        out.append(
            Receivable(
                id=doc.id,
                tenant_id=tenant_id,
                counterparty=group,
                amount=doc.amount,
                basis=ReceivableBasis(entity_of_document(doc)),
                basis_id=doc.id,
                notes=notes,
            )
        )

    # -- rule 5: settlement lines repeated in another document --------------------------
    dups = duplicate_candidates(all_sale_lines, origins or {})
    line_by_id = {ln.id: ln for ln in lines}
    dup_resolved: dict[str, str] = {}  # line id -> "own" | "attached" | "pending"
    for ln in all_sale_lines:
        earlier = dups.get(ln.id)
        if not earlier:
            continue
        cands = tuple(_ref(e, EvidenceRole.CANDIDATE, None, "") for e in earlier)
        confirmations = links_by_doc.get(ln.id, [])
        targets = {(lk.relation, lk.settlement_line_id) for lk in confirmations}
        ids = ", ".join(lk.id for lk in confirmations)
        if not confirmations:
            pending(ln, cands, REASON_DUPLICATE_LINE, PendingKind.DUPLICATE_LINE)
            dup_resolved[ln.id] = "pending"
        elif len(targets) > 1:
            pending(
                ln,
                cands,
                f"{REASON_CONFLICTING_CONFIRMATIONS} [{ids}]",
                PendingKind.DUPLICATE_LINE,
            )
            dup_resolved[ln.id] = "pending"
        else:
            relation, target = next(iter(targets))
            target_line = line_by_id.get(target or "")
            if relation is LinkRelation.SEPARATE_SALE:
                dup_resolved[ln.id] = "own"
            elif (
                target_line is not None
                and target_line.line_kind is LineKind.SALE
                and target not in dups
            ):
                attached.setdefault(target_line.id, []).append(
                    _ref(ln, EvidenceRole.CORROBORATING, LinkMethod.USER, f"사용자 확인 [{ids}]")
                )
                dup_resolved[ln.id] = "attached"
            else:
                pending(
                    ln,
                    cands,
                    f"사용자 확인 [{ids}]의 정산 행 [{target}]이"
                    " 같은 거래처의 기준 판매 정산 행이 아님",
                    PendingKind.DUPLICATE_LINE,
                )
                dup_resolved[ln.id] = "pending"

    # invoices are linked only to settlement SALE lines that are receivables of their own
    sale_lines = [ln for ln in all_sale_lines if dup_resolved.get(ln.id, "own") == "own"]
    sale_by_id = {ln.id: ln for ln in sale_lines}
    by_key: dict[str, list[SettlementLine]] = {}
    for ln in sale_lines:
        k = normalize_ref(ln.reference)
        if k:
            by_key.setdefault(k, []).append(ln)
    all_candidates = tuple(_ref(ln, EvidenceRole.CANDIDATE, None, "") for ln in sale_lines)

    for inv in invoices:
        unknown = _direction_unknown(inv)
        if unknown:
            # rule 6: before the direction is known, no confirmation can make it a receivable
            pending(inv, (), REASON_DIRECTION, PendingKind.INVOICE_DIRECTION, unknown)
            continue
        confirmations = links_by_doc.get(inv.id, [])
        if confirmations:
            ids = ", ".join(lk.id for lk in confirmations)
            targets = {(lk.relation, lk.settlement_line_id) for lk in confirmations}
            if len(targets) > 1:
                pending(inv, all_candidates, f"{REASON_CONFLICTING_CONFIRMATIONS} [{ids}]")
                continue
            relation, line_id = next(iter(targets))
            if relation is LinkRelation.SEPARATE_SALE:
                own(inv, (f"사용자 확인 [{ids}]: 정산 행과 별개의 거래",))
            elif line_id in sale_by_id:
                attached.setdefault(line_id, []).append(
                    _ref(inv, EvidenceRole.CORROBORATING, LinkMethod.USER, f"사용자 확인 [{ids}]")
                )
            else:
                pending(
                    inv,
                    all_candidates,
                    f"사용자 확인 [{ids}]의 정산 행 [{line_id}]이"
                    " 같은 거래처의 판매 정산 행이 아님",
                )
            continue
        key = normalize_ref(inv.reference)
        hits = by_key.get(key, []) if key else []
        if len(hits) == 1:
            attached.setdefault(hits[0].id, []).append(
                _ref(
                    inv,
                    EvidenceRole.CORROBORATING,
                    LinkMethod.REFERENCE,
                    f"같은 거래처·같은 참조번호 [{inv.reference}]",
                )
            )
        elif hits:
            pending(
                inv,
                tuple(_ref(ln, EvidenceRole.CANDIDATE, None, "") for ln in hits),
                f"{REASON_MULTI_REF} [{key}]",
            )
        elif sale_lines:
            pending(inv, all_candidates, REASON_NO_REF)
        else:
            own(inv)

    for ln in lines:
        state = dup_resolved.get(ln.id)
        if state in ("pending", "attached"):
            continue  # a pending duplicate was emitted above; an attached one is evidence
        notes: tuple[str, ...] = ()
        if state == "own":
            ids = ", ".join(lk.id for lk in links_by_doc.get(ln.id, []))
            notes = (f"사용자 확인 [{ids}]: 다른 문서의 같은 참조번호 행과 별개의 거래",)
        out.append(
            Receivable(
                id=ln.id,
                tenant_id=tenant_id,
                counterparty=group,
                amount=ln.amount,
                basis=ReceivableBasis.SETTLEMENT_LINE,
                basis_id=ln.id,
                evidence=tuple(sorted(attached.get(ln.id, ()), key=lambda r: r.id)),
                candidates=tuple(sorted(pending_on_line.get(ln.id, ()), key=lambda r: r.id)),
                notes=notes,
            )
        )
    return tuple(sorted(out, key=lambda r: r.id))
