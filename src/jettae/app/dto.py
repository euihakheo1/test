"""Use-case result objects (plain frozen dataclasses; API/MCP layers map them to JSON)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jettae.domain.models import Approval, Decision, RecomputePlan
from jettae.domain.status import ReconcileStatus, ReviewStatus
from jettae.verify.checks import CheckResult


@dataclass(frozen=True)
class DecisionView:
    decision: Decision
    review_status: ReviewStatus
    approvals: tuple[Approval, ...]
    explanation: str
    checks: tuple[CheckResult, ...]

    @property
    def verified(self) -> bool:
        return all(c.passed for c in self.checks)


@dataclass(frozen=True)
class AnalysisRun:
    tenant_id: str
    snapshot_hash: str
    decisions: tuple[DecisionView, ...]
    unattributed: tuple[tuple[str, str], ...]
    payments: Mapping[str, Any]


@dataclass(frozen=True)
class ChangeOutcome:
    plan: RecomputePlan
    recomputed_groups: frozenset[str]
    changed: tuple[str, ...]  # decisions whose result hash changed (or new)
    review_required: tuple[str, ...]  # previously approved, approval no longer current
    removed: tuple[str, ...]
    snapshot_hash: str
    equivalent_to_full: bool | None = None  # set when verify_full=True


@dataclass(frozen=True)
class RequiredDocuments:
    decision_id: str
    subject_id: str
    status: ReconcileStatus
    missing: tuple[str, ...]
    required_documents: tuple[str, ...]
    unresolved: tuple[str, ...]


@dataclass(frozen=True)
class SourceRef:
    fact_id: str
    doc_version_id: str
    locator: Mapping[str, Any]
    excerpt: str


@dataclass(frozen=True)
class ReportItem:
    decision_id: str
    subject_id: str
    status: ReconcileStatus
    review_status: ReviewStatus
    result_hash: str
    snapshot_hash: str
    explanation: str
    sources: tuple[SourceRef, ...]
    checks: tuple[CheckResult, ...]


@dataclass(frozen=True)
class Report:
    tenant_id: str
    generated_at: datetime
    items: tuple[ReportItem, ...]
    all_approved: bool

    def to_rows(self) -> list[list[str]]:
        """Tabular export with CSV formula-injection protection."""
        rows = [
            ["decision_id", "subject_id", "status", "review_status", "result_hash", "explanation"]
        ]
        for it in self.items:
            rows.append(
                [
                    csv_safe(x)
                    for x in (
                        it.decision_id,
                        it.subject_id,
                        it.status.value,
                        it.review_status.value,
                        it.result_hash,
                        it.explanation,
                    )
                ]
            )
        return rows


_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    """Prefix values that a spreadsheet would interpret as a formula."""
    s = str(value)
    return "'" + s if s.startswith(_FORMULA_PREFIX) else s
