"""Input snapshots, query scopes and their result-set hashes.

A :class:`Snapshot` is an immutable, tenant-scoped view of all records + analysis config.
A :class:`QueryScope` names a query whose *result set* a decision relied on (including an
empty result, e.g. "no agreement found for counterparty X"); its hash is recomputed on a
new snapshot to detect changes that direct fact edges cannot see (inserted records).

Document rows vs economic receivables: ``invoices`` and ``settlement_lines`` are document
rows. :attr:`Snapshot.economic_receivables` groups them into receivables (one per sale; see
:mod:`jettae.evidence.links`). :attr:`Snapshot.receivables` maps each receivable id to its
*basis document* and is the set of decision subjects; a tax invoice that corroborates a
settlement line is in :attr:`Snapshot.documents` but not in :attr:`Snapshot.receivables`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from typing import Any

from jettae.domain.errors import DomainValidationError, TenantMismatchError
from jettae.domain.hashing import content_hash
from jettae.domain.models import (
    Agreement,
    BankTxn,
    Change,
    EvidenceLink,
    Fact,
    Invoice,
    Receivable,
    ReceivableDocument,
    SettlementLine,
)
from jettae.domain.money import RoundingMode
from jettae.domain.status import ChangeKind, LineKind
from jettae.evidence.links import (
    Origin,
    build_group_receivables,
    duplicate_candidates,
    link_group_of,
)
from jettae.recon.candidates import (
    ReconConfig,
    item_from_invoice,
    item_from_line,
    normalize_counterparty,
    payment_from_txn,
    reference_hits,
)
from jettae.rules.calendar_kr import KrCalendar, default_calendar
from jettae.rules.kr_retail import builtin_registry
from jettae.rules.registry import RuleRegistry

RECORD_FIELDS = {
    "invoice": "invoices",
    "settlement_line": "settlement_lines",
    "bank_txn": "bank_txns",
    "agreement": "agreements",
    "evidence_link": "evidence_links",
    "fact": "facts",
}
RECORD_TYPES: dict[str, type[Any]] = {
    "invoice": Invoice,
    "settlement_line": SettlementLine,
    "bank_txn": BankTxn,
    "agreement": Agreement,
    "evidence_link": EvidenceLink,
    "fact": Fact,
}


def decision_id_for(subject_id: str) -> str:
    return f"dec:{subject_id}"


def entity_of(record: Any) -> str:
    for name, typ in RECORD_TYPES.items():
        if isinstance(record, typ):
            return name
    raise TypeError(f"not a snapshot record: {type(record).__name__}")


@dataclass(frozen=True)
class AnalysisConfig:
    # "Unpaid as of" date: open amounts accrue delay up to it and bank transactions booked
    # after it are excluded. It does NOT restore the documents/records known on that date
    # (no bitemporal replay of the ledger); the snapshot's records are used as they are.
    as_of: date | None = None
    rollover: bool | None = None  # None = unresolved unless an agreement fixes it
    rounding: RoundingMode = RoundingMode.FLOOR
    # Rule selection only: rule versions published (known_from) after this date are ignored.
    known_at: date | None = None
    recon: ReconConfig = field(default_factory=ReconConfig)
    registry: RuleRegistry = field(default_factory=builtin_registry, compare=False)
    calendar: KrCalendar = field(default_factory=default_calendar, compare=False)

    def fingerprint(self, *, include_as_of: bool = True) -> str:
        """Hash of the configuration. ``include_as_of=False`` gives the hash of everything
        except the reference date (the ``config`` query scope): decisions whose outputs do
        not depend on ``as_of`` (fully paid, withheld, insufficient) must not be invalidated
        merely because the analysis ran on a later day."""
        rc = self.recon
        return content_hash(
            {
                "as_of": self.as_of if include_as_of else "<excluded>",
                "rollover": self.rollover,
                "rounding": self.rounding,
                "known_at": self.known_at,
                "recon": {
                    "days_before": rc.days_before,
                    "days_after": rc.days_after,
                    "fee_tolerance": rc.fee_tolerance,
                    "max_candidates": rc.max_candidates,
                    "max_subset_size": rc.max_subset_size,
                    "max_nodes": rc.max_nodes,
                },
                "registry": self.registry.fingerprint(),
                "calendar": self.calendar.fingerprint(),
            }
        )


@dataclass(frozen=True, order=True)
class QueryScope:
    kind: str  # receivables | bank_txns | agreements | config | as_of
    key: str = ""

    def __str__(self) -> str:
        return f"{self.kind}:{self.key}"

    @classmethod
    def parse(cls, s: str) -> QueryScope:
        kind, _, key = s.partition(":")
        return cls(kind, key)


SCOPE_KINDS = frozenset({"receivables", "bank_txns", "agreements", "config", "as_of"})

AFTER_AS_OF_REASON = "기준일(as_of) 이후 입금: 이번 분석에서 제외"


class UnknownScopeError(LookupError):
    pass


@dataclass(frozen=True)
class Snapshot:
    tenant_id: str
    invoices: tuple[Invoice, ...] = ()
    settlement_lines: tuple[SettlementLine, ...] = ()
    bank_txns: tuple[BankTxn, ...] = ()
    agreements: tuple[Agreement, ...] = ()
    facts: tuple[Fact, ...] = ()
    config: AnalysisConfig = field(default_factory=AnalysisConfig)
    # user confirmations of invoice <-> settlement-line links (see jettae.evidence.links)
    evidence_links: tuple[EvidenceLink, ...] = ()

    def __post_init__(self) -> None:
        receivable_ids: set[str] = set()
        for fname in RECORD_FIELDS.values():
            recs = tuple(sorted(getattr(self, fname), key=lambda r: r.id))
            ids = [r.id for r in recs]
            if len(set(ids)) != len(ids):
                raise DomainValidationError(f"duplicate ids in {fname}")
            for r in recs:
                if r.tenant_id != self.tenant_id:
                    raise TenantMismatchError(
                        f"{fname} {r.id} belongs to another tenant than {self.tenant_id}"
                    )
            if fname in ("invoices", "settlement_lines"):
                if receivable_ids & set(ids):
                    raise DomainValidationError("invoice and settlement line ids overlap")
                receivable_ids.update(ids)
            object.__setattr__(self, fname, recs)

    # -- hashes -------------------------------------------------------------------
    @cached_property
    def snapshot_hash(self) -> str:
        payload: dict[str, Any] = {
            "tenant": self.tenant_id,
            "invoices": self.invoices,
            "settlement_lines": self.settlement_lines,
            "bank_txns": self.bank_txns,
            "agreements": self.agreements,
            "facts": self.facts,
            "config": self.config.fingerprint(),
        }
        if self.evidence_links:  # omitted when empty: hashes of older snapshots stay valid
            payload["evidence_links"] = self.evidence_links
        return content_hash(payload)

    # -- indexes ------------------------------------------------------------------
    @cached_property
    def documents(self) -> dict[str, ReceivableDocument]:
        """All receivable-side document rows (invoices + settlement lines) by id."""
        out: dict[str, ReceivableDocument] = {r.id: r for r in self.invoices}
        out.update({r.id: r for r in self.settlement_lines})
        return out

    @cached_property
    def economic_receivables(self) -> dict[str, Receivable]:
        """One :class:`Receivable` per sale, plus documents awaiting link confirmation."""
        out: dict[str, Receivable] = {}
        origins = self.document_origins
        for g, docs in self.receivable_groups.items():
            for r in build_group_receivables(
                self.tenant_id, g, docs, self.link_groups.get(g, ()), origins
            ):
                out[r.id] = r
        return dict(sorted(out.items()))

    @cached_property
    def document_origins(self) -> dict[str, Origin]:
        """Provenance of each document row that was read from a file: (registration time of
        the document version its facts cite, that version's id). Used to tell "the same line
        in another document" (a possible duplicate) from two lines of one statement. Rows
        without source spans (hand-entered records) have no entry."""
        idx = self.fact_index
        out: dict[str, Origin] = {}
        for r in self.documents.values():
            spans = sorted(
                (f.observed_at, f.span.doc_version_id)
                for fid in r.facts
                if (f := idx.get(fid)) is not None and f.span is not None
            )
            if spans:
                out[r.id] = spans[0]
        return out

    @cached_property
    def duplicate_groups(self) -> frozenset[str]:
        """Counterparty groups in which some settlement line has a possible duplicate in
        another document (their receivable scope depends on provenance)."""
        origins = self.document_origins
        out = set()
        for g, docs in self.receivable_groups.items():
            sale = [
                d for d in docs if isinstance(d, SettlementLine) and d.line_kind is LineKind.SALE
            ]
            if duplicate_candidates(sale, origins):
                out.add(g)
        return frozenset(out)

    @cached_property
    def receivables(self) -> dict[str, ReceivableDocument]:
        """Decision subjects: receivable id -> basis document.

        One entry per economic receivable, NOT per document row: an invoice linked to a
        settlement line as corroborating evidence is absent. Entries in state
        ``needs_confirmation`` are present (they get an AMBIGUOUS decision) but are not
        counted; use :meth:`counted_total` for amounts."""
        docs = self.documents
        return {rid: docs[r.basis_id] for rid, r in self.economic_receivables.items()}

    def counted_total(self, currency: str = "KRW") -> int:
        """Sum of the amounts of established receivables (minor units)."""
        return sum(
            r.amount.amount
            for r in self.economic_receivables.values()
            if r.counted and r.amount.currency == currency
        )

    @cached_property
    def fact_index(self) -> dict[str, Fact]:
        return {f.id: f for f in self.facts}

    @cached_property
    def receivable_groups(self) -> dict[str, tuple[ReceivableDocument, ...]]:
        """Counterparty group -> its document rows (the unit of recomputation)."""
        groups: dict[str, list[ReceivableDocument]] = {}
        for r in sorted(self.documents.values(), key=lambda r: r.id):
            groups.setdefault(normalize_counterparty(r.counterparty), []).append(r)
        return {g: tuple(v) for g, v in sorted(groups.items())}

    @cached_property
    def link_groups(self) -> dict[str, tuple[EvidenceLink, ...]]:
        """Counterparty group -> user link confirmations whose invoice is in that group."""
        out: dict[str, list[EvidenceLink]] = {}
        docs = self.documents
        for lk in self.evidence_links:
            g = link_group_of(lk, docs)
            if g is not None:
                out.setdefault(g, []).append(lk)
        return {g: tuple(v) for g, v in sorted(out.items())}

    @cached_property
    def receivable_model_groups(self) -> dict[str, tuple[Receivable, ...]]:
        """Counterparty group -> its economic receivables (sorted by id)."""
        out: dict[str, list[Receivable]] = {}
        for r in self.economic_receivables.values():
            out.setdefault(r.counterparty, []).append(r)
        return {g: tuple(v) for g, v in sorted(out.items())}

    @cached_property
    def _attribution(self) -> tuple[dict[str, tuple[BankTxn, ...]], tuple[tuple[str, str], ...]]:
        groups = self.receivable_groups
        # group detection only: every document row's reference identifies its counterparty
        all_items = [
            item_from_invoice(r) if isinstance(r, Invoice) else item_from_line(r)
            for r in self.documents.values()
        ]
        out: dict[str, list[BankTxn]] = {}
        unattributed: list[tuple[str, str]] = []
        as_of = self.config.as_of
        for t in self.bank_txns:
            if as_of is not None and t.booked_date > as_of:
                # A payment booked after the reference date did not exist "as of" that date.
                unattributed.append((t.id, AFTER_AS_OF_REASON))
                continue
            g = normalize_counterparty(t.counterparty)
            if g and g in groups:
                out.setdefault(g, []).append(t)
                continue
            hit_groups = sorted(
                {it.counterparty for it in reference_hits(payment_from_txn(t), all_items)}
            )
            if len(hit_groups) == 1:
                out.setdefault(hit_groups[0], []).append(t)
            elif hit_groups:
                unattributed.append((t.id, "참조번호가 여러 거래처의 거래와 일치"))
            else:
                unattributed.append((t.id, "거래처를 특정할 수 없음(거래처명·참조번호 불일치)"))
        return {g: tuple(v) for g, v in out.items()}, tuple(unattributed)

    @property
    def txn_groups(self) -> dict[str, tuple[BankTxn, ...]]:
        return self._attribution[0]

    @property
    def unattributed_txns(self) -> tuple[tuple[str, str], ...]:
        return self._attribution[1]

    @cached_property
    def agreement_groups(self) -> dict[str, tuple[Agreement, ...]]:
        out: dict[str, list[Agreement]] = {}
        for a in self.agreements:
            out.setdefault(normalize_counterparty(a.counterparty), []).append(a)
        return {g: tuple(v) for g, v in out.items()}

    def group_of(self, subject_id: str) -> str | None:
        r = self.documents.get(subject_id)
        return None if r is None else normalize_counterparty(r.counterparty)

    # -- scopes ---------------------------------------------------------------------
    def evaluate_scope(self, scope: QueryScope) -> str:
        """Result-set hash of ``scope`` on this snapshot (content of matching records)."""
        if scope.kind == "receivables":
            docs = self.receivable_groups.get(scope.key, ())
            links = self.link_groups.get(scope.key, ())
            payload: Any = docs if not links else {"documents": docs, "links": links}
            if scope.key in self.duplicate_groups:
                # which line is "earlier" depends on provenance (see links rule 5)
                origins = self.document_origins
                payload = {
                    "documents": docs,
                    "links": links,
                    "origins": {d.id: origins.get(d.id) for d in docs},
                }
            # links/origins are hashed only when relevant so existing stored hashes stay valid
            return content_hash(payload)
        if scope.kind == "bank_txns":
            return content_hash(self.txn_groups.get(scope.key, ()))
        if scope.kind == "agreements":
            return content_hash(self.agreement_groups.get(scope.key, ()))
        if scope.kind == "config":
            return self.config.fingerprint(include_as_of=False)
        if scope.kind == "as_of":
            return content_hash({"as_of": self.config.as_of})
        raise UnknownScopeError(str(scope))

    # -- changes --------------------------------------------------------------------
    def apply(self, change: Change) -> Snapshot:
        """Return a new snapshot with ``change`` applied (records only; config via
        ``dataclasses.replace(snapshot, config=...)``)."""
        if change.entity == "config":
            if not isinstance(change.record, AnalysisConfig):
                raise DomainValidationError("config change requires an AnalysisConfig record")
            return dataclasses.replace(self, config=change.record)
        fname = RECORD_FIELDS.get(change.entity)
        if fname is None:
            raise DomainValidationError(f"cannot apply change to entity {change.entity!r}")
        current: tuple[Any, ...] = getattr(self, fname)
        exists = any(r.id == change.entity_id for r in current)
        kind = ChangeKind(change.kind)
        if kind is ChangeKind.REMOVE:
            if not exists:
                raise DomainValidationError(f"{change.entity} {change.entity_id} not found")
            new = tuple(r for r in current if r.id != change.entity_id)
        else:
            rec: Any = change.record
            if not isinstance(rec, RECORD_TYPES[change.entity]) or rec.id != change.entity_id:
                raise DomainValidationError("change.record must match entity and entity_id")
            if kind is ChangeKind.ADD and exists:
                raise DomainValidationError(f"{change.entity} {change.entity_id} already exists")
            if kind is ChangeKind.UPDATE and not exists:
                raise DomainValidationError(f"{change.entity} {change.entity_id} not found")
            new = tuple(r for r in current if r.id != change.entity_id) + (rec,)
        return dataclasses.replace(self, **{fname: new})  # type: ignore[arg-type]

    def apply_all(self, changes: list[Change] | tuple[Change, ...]) -> Snapshot:
        snap = self
        for c in changes:
            snap = snap.apply(c)
        return snap

    def records(self) -> Mapping[str, tuple[Any, ...]]:
        return {name: getattr(self, f) for name, f in RECORD_FIELDS.items()}
