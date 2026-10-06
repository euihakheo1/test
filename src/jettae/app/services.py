"""Application use cases. API, MCP, CLI and worker call these; they depend on ports only."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime
from typing import Any

from jettae.app.doc_apply import DocumentAckRequiredError, approval_blockers
from jettae.app.dto import (
    AnalysisRun,
    ChangeOutcome,
    DecisionView,
    Report,
    ReportItem,
    RequiredDocuments,
    SourceRef,
)
from jettae.app.explain import render_explanation
from jettae.app.ports import STORED_ENTITIES, Repositories
from jettae.domain.dates import DISPLAY_TZ, ensure_utc, utc_now
from jettae.domain.errors import (
    DomainValidationError,
    NotFoundError,
    ReportValidityError,
    StaleResultError,
    TenantMismatchError,
)
from jettae.domain.hashing import bytes_hash
from jettae.domain.models import (
    Approval,
    Change,
    Decision,
    DocumentVersion,
    EvidenceLink,
    Fact,
    Invoice,
    LinkRelation,
    PendingKind,
    RecomputePlan,
    SettlementLine,
)
from jettae.domain.status import ChangeKind, DocKind, DocumentStatus, LineKind, ReviewStatus
from jettae.evidence.engine import AnalysisResult, full_recompute, incremental_recompute
from jettae.evidence.snapshot import AnalysisConfig, Snapshot, entity_of
from jettae.recon.candidates import normalize_counterparty
from jettae.verify.checks import CheckResult, check_citation, check_explanation, check_wording


class EvidenceLinkError(DomainValidationError):
    """A confirmation of how a tax invoice relates to settlement lines was rejected
    (``code`` names the reason; the API answers 422 with it)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def evidence_link_id(invoice_id: str) -> str:
    """One confirmation record per invoice: a new confirmation replaces the previous one,
    so two contradicting confirmations of the same invoice are never stored together."""
    return f"elink:{invoice_id}"


def compute_review_status(
    decision: Decision | None,
    approvals: Sequence[Approval],
    *,
    verified: bool,
    superseded: bool = False,
) -> ReviewStatus:
    """Current validity. Approvals are never modified; validity is derived from them."""
    if superseded or decision is None:
        return ReviewStatus.SUPERSEDED
    if any(a.result_hash == decision.result_hash for a in approvals):
        return ReviewStatus.APPROVED
    if approvals:
        return ReviewStatus.REVIEW_REQUIRED
    return ReviewStatus.VERIFIED if verified else ReviewStatus.DRAFT


class JettaeService:
    def __init__(
        self,
        repos: Repositories,
        *,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self.repos = repos
        self.clock = clock
        self._new_id = id_factory or (lambda prefix: f"{prefix}_{uuid.uuid4().hex}")

    # ------------------------------------------------------------------ documents / facts
    def register_document(
        self,
        tenant_id: str,
        *,
        filename: str,
        content: bytes,
        media_type: str,
        kind: DocKind = DocKind.OTHER,
        document_id: str | None = None,
        text: str | None = None,
        status: DocumentStatus = DocumentStatus.REGISTERED,
    ) -> DocumentVersion:
        """Store a new document version (content-addressed). Re-uploading identical content
        for the same document returns the existing version."""
        h = bytes_hash(content)
        with self.repos.uow.transaction(tenant_id):
            latest = self.repos.documents.latest(tenant_id, document_id) if document_id else None
            if latest is not None and latest.content_hash == h:
                return latest
            key = self.repos.blobs.put(tenant_id, content)
            doc = DocumentVersion(
                id=self._new_id("docv"),
                tenant_id=tenant_id,
                document_id=document_id or self._new_id("doc"),
                version=(latest.version + 1) if latest else 1,
                content_hash=h,
                filename=filename,
                media_type=media_type,
                kind=kind,
                created_at=ensure_utc(self.clock()),
                size=len(content),
                supersedes=latest.id if latest else None,
                status=status,
                storage_key=key,
            )
            self.repos.documents.add(doc, text)
            return doc

    def record_facts(
        self,
        tenant_id: str,
        facts: Iterable[Fact] = (),
        records: Iterable[Any] = (),
    ) -> list[Change]:
        """Persist extracted facts and ledger records. Returns the equivalent changes
        (pass them to :meth:`apply_change` semantics via :meth:`run_analysis` or use
        :meth:`apply_change` directly for incremental recompute)."""
        changes: list[Change] = []
        with self.repos.uow.transaction(tenant_id):
            for f in facts:
                self._check_tenant(tenant_id, f)
                kind = (
                    ChangeKind.UPDATE if self.repos.facts.get(tenant_id, f.id) else ChangeKind.ADD
                )
                self.repos.facts.upsert(f)
                changes.append(Change(kind, "fact", f.id, f))
            for r in records:
                self._check_tenant(tenant_id, r)
                ent = entity_of(r)
                exists = self.repos.ledger.get(tenant_id, ent, r.id) is not None
                self.repos.ledger.upsert(ent, r)
                changes.append(
                    Change(ChangeKind.UPDATE if exists else ChangeKind.ADD, ent, r.id, r)
                )
        return changes

    # ------------------------------------------------------------------ analysis
    def snapshot(self, tenant_id: str, config: AnalysisConfig) -> Snapshot:
        L = self.repos.ledger
        return Snapshot(
            tenant_id=tenant_id,
            invoices=tuple(L.list(tenant_id, "invoice")),
            settlement_lines=tuple(L.list(tenant_id, "settlement_line")),
            bank_txns=tuple(L.list(tenant_id, "bank_txn")),
            agreements=tuple(L.list(tenant_id, "agreement")),
            evidence_links=tuple(L.list(tenant_id, "evidence_link")),
            facts=tuple(self.repos.facts.list(tenant_id)),
            config=config,
        )

    def _today(self) -> date:
        now = ensure_utc(self.clock())
        return now.astimezone(DISPLAY_TZ).date() if DISPLAY_TZ else now.date()

    def run_analysis(
        self, tenant_id: str, *, as_of: date | None = None, config: AnalysisConfig | None = None
    ) -> AnalysisRun:
        """Deterministic full recompute of the tenant's current snapshot."""
        with self.repos.uow.transaction(tenant_id):
            cfg = config or AnalysisConfig(as_of=as_of or self._today())
            snap = self.snapshot(tenant_id, cfg)
            result = full_recompute(snap)
            self._save(tenant_id, result, cfg)
            return self._run_view(tenant_id, result)

    def apply_change(
        self,
        tenant_id: str,
        changes: Sequence[Change],
        *,
        verify_full: bool = False,
        as_of: date | None = None,
    ) -> ChangeOutcome:
        """Apply record/fact changes and recompute incrementally (falls back to full).

        The stored analysis config is reused, but its reference date is re-based to
        ``as_of`` (default: today in Asia/Seoul) unless a ``config`` change in ``changes``
        sets one explicitly. Only decisions with an open (unpaid) tranche depend on
        ``as_of``, so the planner recomputes exactly those (scope ``as_of``).

        With ``verify_full=True`` the incremental result is compared with a full recompute
        of the same snapshot (used by the E6 evaluation)."""
        with self.repos.uow.transaction(tenant_id):
            loaded = self.repos.results.load(tenant_id)
            for c in changes:
                self._apply_one(tenant_id, c)
            if loaded is None:
                cfg = AnalysisConfig(as_of=as_of or self._today())
                snap = self.snapshot(tenant_id, cfg)
                prev_ids: set[str] = set(self.repos.decisions.current(tenant_id))
                new = full_recompute(snap)
                plan = RecomputePlan(
                    affected=frozenset(new.decisions),
                    fallback_full=True,
                    notes=("이전 분석 결과 없음: 전체 재계산",),
                )
                groups = frozenset(snap.receivable_groups)
            else:
                prev, cfg = loaded
                explicit = [
                    c.record
                    for c in changes
                    if c.entity == "config" and isinstance(c.record, AnalysisConfig)
                ]
                if explicit:
                    cfg = explicit[-1]
                else:
                    target = as_of or self._today()
                    if cfg.as_of != target:
                        cfg = dataclasses.replace(cfg, as_of=target)
                snap = self.snapshot(tenant_id, cfg)
                prev_ids = set(prev.decisions)
                new, plan, groups = incremental_recompute(prev, snap, changes)
            before = self.repos.decisions.current(tenant_id)
            approved_before = {
                d
                for d, dec in before.items()
                if any(
                    a.result_hash == dec.result_hash
                    for a in self.repos.approvals.list_for(tenant_id, d)
                )
            }
            self._save(tenant_id, new, cfg)
            changed = tuple(
                sorted(
                    d
                    for d, dec in new.decisions.items()
                    if d not in before or before[d].result_hash != dec.result_hash
                )
            )
            review_required = tuple(
                sorted(
                    d
                    for d in approved_before
                    if d in new.decisions and before[d].result_hash != new.decisions[d].result_hash
                )
            )
            equivalent = None
            if verify_full:
                equivalent = full_recompute(snap).comparable() == new.comparable()
            return ChangeOutcome(
                plan=plan,
                recomputed_groups=groups,
                changed=changed,
                review_required=review_required,
                removed=tuple(sorted(prev_ids - set(new.decisions))),
                snapshot_hash=new.snapshot_hash,
                equivalent_to_full=equivalent,
            )

    def _apply_one(self, tenant_id: str, c: Change) -> None:
        if c.entity in STORED_ENTITIES:
            if c.kind is ChangeKind.REMOVE:
                if self.repos.ledger.get(tenant_id, c.entity, c.entity_id) is None:
                    raise NotFoundError(f"{c.entity} {c.entity_id}")
                self.repos.ledger.remove(tenant_id, c.entity, c.entity_id)
            else:
                self._check_tenant(tenant_id, c.record)
                if entity_of(c.record) != c.entity or c.record.id != c.entity_id:
                    raise DomainValidationError("change record does not match entity/id")
                self.repos.ledger.upsert(c.entity, c.record)
        elif c.entity == "fact":
            if c.kind is ChangeKind.REMOVE:
                self.repos.facts.remove(tenant_id, c.entity_id)
            else:
                self._check_tenant(tenant_id, c.record)
                self.repos.facts.upsert(c.record)
        elif c.entity in ("doc_version", "config"):
            pass  # documents are registered separately; config is read in apply_change
        else:
            # Unknown entity: nothing to store; the planner will fall back to full recompute.
            pass

    def _save(self, tenant_id: str, result: AnalysisResult, cfg: AnalysisConfig) -> None:
        self.repos.decisions.save_current(tenant_id, result.decisions, ensure_utc(self.clock()))
        self.repos.results.save(tenant_id, result, cfg)

    # ------------------------------------------------------------------ evidence links
    def confirm_evidence_link(
        self,
        tenant_id: str,
        decision_id: str,
        expected_result_hash: str,
        *,
        relation: LinkRelation | str,
        confirmed_by: str,
        settlement_line_id: str | None = None,
        invoice_id: str | None = None,
        note: str = "",
        as_of: date | None = None,
    ) -> tuple[EvidenceLink, ChangeOutcome]:
        """Record the user's answer to "is this tax invoice the same sale as a settlement
        line?" and recompute incrementally.

        The decision named here must be the one the user reviewed (``expected_result_hash``,
        optimistic concurrency like :meth:`approve`). ``invoice_id`` defaults to the
        decision's basis invoice (the AMBIGUOUS decision with the ``evidence_link`` item); it
        may also name an invoice the decision lists as corroborating or candidate evidence,
        so a reference-based link can be overridden. ``same_sale`` needs a SALE settlement
        line of the same counterparty; ``separate_sale`` makes the invoice its own
        receivable. ``confirmed_by`` comes from the caller's authentication, never from a
        request body. Amounts are not compared here: a confirmed link between documents
        with different amounts is reported by the engine as CONFLICT.
        """
        rel = LinkRelation(relation)
        with self.repos.uow.transaction(tenant_id):
            dec = self.repos.decisions.get_current(tenant_id, decision_id)
            if dec is None:
                raise NotFoundError(decision_id)
            if dec.result_hash != expected_result_hash:
                raise StaleResultError(decision_id, expected_result_hash, dec.result_hash)
            ev = dec.computation("evidence")
            if ev is None:
                raise EvidenceLinkError(
                    "no_evidence_model", "this decision has no document-evidence computation"
                )
            conf = ev.outputs.get("confirmation_required") or {}
            pending = conf.get("kind") if isinstance(conf, dict) else None
            if (
                invoice_id is None
                and ev.outputs.get("basis") == "settlement_line"
                and pending == PendingKind.DUPLICATE_LINE.value
            ):
                return self._confirm_duplicate_line(
                    tenant_id,
                    str(ev.outputs["basis_id"]),
                    [str(x) for x in conf.get("candidate_settlement_lines", ())],
                    rel,
                    confirmed_by,
                    settlement_line_id,
                    note,
                    as_of,
                )
            listed = [*ev.outputs.get("documents", ()), *ev.outputs.get("candidates", ())]
            allowed = {str(d["id"]) for d in listed if d.get("entity") == "invoice"}
            if ev.outputs.get("basis") == "invoice":
                allowed.add(str(ev.outputs["basis_id"]))
            if invoice_id is None:
                if ev.outputs.get("basis") != "invoice":
                    raise EvidenceLinkError(
                        "invoice_required",
                        "the decision's basis is a settlement line; name the invoice to confirm",
                    )
                invoice_id = str(ev.outputs["basis_id"])
            if invoice_id not in allowed:
                raise EvidenceLinkError(
                    "invoice_not_in_decision", "the invoice is not evidence of this decision"
                )
            inv = self.repos.ledger.get(tenant_id, "invoice", invoice_id)
            if not isinstance(inv, Invoice):
                raise NotFoundError(f"invoice {invoice_id}")
            if "direction" in inv.missing or not normalize_counterparty(inv.counterparty):
                # Not known to be a sales invoice at all: no "same/separate sale" answer can
                # make it a receivable. The fix is to re-read the file with the direction.
                raise EvidenceLinkError(
                    "direction_unknown",
                    "the invoice's direction (sales/purchase) or counterparty is unknown; "
                    "confirm the mapping with self_brn or direction and re-read the document",
                )
            if rel is LinkRelation.SAME_SALE:
                if not settlement_line_id:
                    raise EvidenceLinkError(
                        "settlement_line_required", "same_sale needs settlement_line_id"
                    )
                line = self.repos.ledger.get(tenant_id, "settlement_line", settlement_line_id)
                if not isinstance(line, SettlementLine):
                    raise EvidenceLinkError("unknown_settlement_line", "settlement line not found")
                if normalize_counterparty(line.counterparty) != normalize_counterparty(
                    inv.counterparty
                ):
                    raise EvidenceLinkError(
                        "other_counterparty", "the settlement line belongs to another counterparty"
                    )
                if line.line_kind is not LineKind.SALE:
                    raise EvidenceLinkError(
                        "not_a_sale_line",
                        "deduction/return/fee lines are never linked to invoices",
                    )
            elif settlement_line_id:
                raise EvidenceLinkError(
                    "unexpected_settlement_line", "separate_sale must not name a settlement line"
                )
            link = EvidenceLink(
                id=evidence_link_id(invoice_id),
                tenant_id=tenant_id,
                invoice_id=invoice_id,
                relation=rel,
                settlement_line_id=settlement_line_id if rel is LinkRelation.SAME_SALE else None,
                confirmed_by=confirmed_by,
                note=note,
            )
            return link, self._store_link(tenant_id, link, as_of)

    def _store_link(self, tenant_id: str, link: EvidenceLink, as_of: date | None) -> ChangeOutcome:
        existing = self.repos.ledger.get(tenant_id, "evidence_link", link.id)
        kind = ChangeKind.UPDATE if existing is not None else ChangeKind.ADD
        return self.apply_change(
            tenant_id, [Change(kind, "evidence_link", link.id, link)], as_of=as_of
        )

    def _confirm_duplicate_line(
        self,
        tenant_id: str,
        line_id: str,
        candidates: list[str],
        rel: LinkRelation,
        confirmed_by: str,
        settlement_line_id: str | None,
        note: str,
        as_of: date | None,
    ) -> tuple[EvidenceLink, ChangeOutcome]:
        """Answer "is this settlement line the same line as the earlier document's line?"
        (``evidence.links`` rule 5). ``same_sale`` must name one of the candidate lines (the
        line becomes corroborating evidence of it, adding no amount); ``separate_sale``
        makes it a counted receivable of its own. Runs inside the caller's transaction."""
        line = self.repos.ledger.get(tenant_id, "settlement_line", line_id)
        if not isinstance(line, SettlementLine):
            raise NotFoundError(f"settlement line {line_id}")
        if rel is LinkRelation.SAME_SALE:
            if not settlement_line_id:
                raise EvidenceLinkError(
                    "settlement_line_required", "same_sale needs settlement_line_id"
                )
            if settlement_line_id not in candidates:
                raise EvidenceLinkError(
                    "unknown_settlement_line",
                    "same_sale must name one of the decision's candidate settlement lines",
                )
        elif settlement_line_id:
            raise EvidenceLinkError(
                "unexpected_settlement_line", "separate_sale must not name a settlement line"
            )
        link = EvidenceLink(
            id=evidence_link_id(line_id),
            tenant_id=tenant_id,
            invoice_id=line_id,
            relation=rel,
            settlement_line_id=settlement_line_id if rel is LinkRelation.SAME_SALE else None,
            confirmed_by=confirmed_by,
            note=note,
            document_entity="settlement_line",
        )
        return link, self._store_link(tenant_id, link, as_of)

    def withdraw_evidence_link(
        self, tenant_id: str, link_id: str, *, as_of: date | None = None
    ) -> ChangeOutcome:
        """Remove a confirmation; the invoice falls back to the automatic linking rules
        (explicit reference match, otherwise "needs confirmation")."""
        with self.repos.uow.transaction(tenant_id):
            if self.repos.ledger.get(tenant_id, "evidence_link", link_id) is None:
                raise NotFoundError(link_id)
            return self.apply_change(
                tenant_id, [Change(ChangeKind.REMOVE, "evidence_link", link_id)], as_of=as_of
            )

    def list_evidence_links(self, tenant_id: str) -> list[EvidenceLink]:
        return list(self.repos.ledger.list(tenant_id, "evidence_link"))

    # ------------------------------------------------------------------ approvals
    def approve(
        self, tenant_id: str, decision_id: str, expected_result_hash: str, approved_by: str
    ) -> Approval:
        """Approve the *current* result. Raises StaleResultError when the expected hash is
        not the current one (optimistic concurrency), and DocumentAckRequiredError when the
        decision cites a document version that was applied only partially (rows excluded,
        values unread or totals differing) and nobody acknowledged that yet."""
        with self.repos.uow.transaction(tenant_id):
            dec = self.repos.decisions.get_current(tenant_id, decision_id)
            if dec is None:
                raise NotFoundError(decision_id)
            if dec.result_hash != expected_result_hash:
                raise StaleResultError(decision_id, expected_result_hash, dec.result_hash)
            blockers = approval_blockers(self.repos, tenant_id, dec)
            if blockers:
                raise DocumentAckRequiredError(decision_id, blockers)
            appr = Approval(
                decision_id=decision_id,
                result_hash=dec.result_hash,
                snapshot_hash=dec.snapshot_hash,
                approved_by=approved_by,
                approved_at=ensure_utc(self.clock()),
                tenant_id=tenant_id,
                id=self._new_id("appr"),
            )
            self.repos.approvals.add(appr)
            return appr

    # ------------------------------------------------------------------ views
    def _checks(self, tenant_id: str, dec: Decision, explanation: str) -> tuple[CheckResult, ...]:
        checks = [check_explanation(explanation, dec), check_wording(explanation)]
        for fid in dec.facts_used:
            f = self.repos.facts.get(tenant_id, fid)
            if f is None or f.span is None:
                continue
            text = self.repos.documents.get_text(tenant_id, f.span.doc_version_id)
            if text is not None:
                checks.append(check_citation(f.span, text))
        return tuple(checks)

    def decision_view(self, tenant_id: str, decision_id: str) -> DecisionView:
        dec = self.repos.decisions.get_current(tenant_id, decision_id)
        approvals = tuple(self.repos.approvals.list_for(tenant_id, decision_id))
        if dec is None:
            hist = self.repos.decisions.history(tenant_id, decision_id)
            if not hist:
                raise NotFoundError(decision_id)
            last = hist[-1].decision
            expl = render_explanation(last)
            return DecisionView(last, ReviewStatus.SUPERSEDED, approvals, expl, ())
        expl = render_explanation(dec)
        checks = self._checks(tenant_id, dec, expl)
        status = compute_review_status(
            dec,
            approvals,
            verified=all(c.passed for c in checks),
            superseded=self.repos.decisions.is_superseded(tenant_id, decision_id),
        )
        return DecisionView(dec, status, approvals, expl, checks)

    def list_decisions(self, tenant_id: str) -> list[DecisionView]:
        return [
            self.decision_view(tenant_id, d)
            for d in sorted(self.repos.decisions.current(tenant_id))
        ]

    def _run_view(self, tenant_id: str, result: AnalysisResult) -> AnalysisRun:
        return AnalysisRun(
            tenant_id=tenant_id,
            snapshot_hash=result.snapshot_hash,
            decisions=tuple(self.decision_view(tenant_id, d) for d in sorted(result.decisions)),
            unattributed=result.unattributed,
            payments={pid: pr for g in result.groups.values() for pid, pr in g.payments.items()},
        )

    def list_required_documents(self, tenant_id: str) -> list[RequiredDocuments]:
        out = []
        for d, dec in sorted(self.repos.decisions.current(tenant_id).items()):
            if dec.required_documents or dec.missing or dec.unresolved:
                out.append(
                    RequiredDocuments(
                        d,
                        dec.subject_id,
                        dec.status,
                        dec.missing,
                        dec.required_documents,
                        dec.unresolved,
                    )
                )
        return out

    def export_report(
        self, tenant_id: str, decision_ids: Iterable[str], *, require_approved: bool = False
    ) -> Report:
        """Build a report, re-checking each decision's *current* validity at export time."""
        items: list[ReportItem] = []
        invalid: dict[str, str] = {}
        for did in decision_ids:
            view = self.decision_view(tenant_id, did)
            if view.review_status is not ReviewStatus.APPROVED:
                invalid[did] = view.review_status.value
            dec = view.decision
            sources = []
            for fid in dec.facts_used:
                f = self.repos.facts.get(tenant_id, fid)
                if f is not None and f.span is not None:
                    sources.append(
                        SourceRef(fid, f.span.doc_version_id, dict(f.span.locator), f.span.excerpt)
                    )
            items.append(
                ReportItem(
                    did,
                    dec.subject_id,
                    dec.status,
                    view.review_status,
                    dec.result_hash,
                    dec.snapshot_hash,
                    view.explanation,
                    tuple(sources),
                    view.checks,
                )
            )
        if require_approved and invalid:
            raise ReportValidityError(invalid)
        return Report(tenant_id, ensure_utc(self.clock()), tuple(items), not invalid)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _check_tenant(tenant_id: str, obj: Any) -> None:
        if getattr(obj, "tenant_id", None) != tenant_id:
            raise TenantMismatchError(
                f"object {getattr(obj, 'id', '?')} is not in tenant {tenant_id}"
            )
