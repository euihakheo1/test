"""Evidence: snapshots, dependency graph, invalidation planning, full/incremental recompute."""

from jettae.evidence.deps import DependencyGraph
from jettae.evidence.engine import (
    AnalysisResult,
    GroupOutcome,
    analyze_group,
    full_recompute,
    incremental_recompute,
)
from jettae.evidence.invalidate import plan
from jettae.evidence.snapshot import (
    AnalysisConfig,
    QueryScope,
    Snapshot,
    UnknownScopeError,
    decision_id_for,
)

__all__ = [
    "AnalysisConfig",
    "AnalysisResult",
    "DependencyGraph",
    "GroupOutcome",
    "QueryScope",
    "Snapshot",
    "UnknownScopeError",
    "analyze_group",
    "decision_id_for",
    "full_recompute",
    "incremental_recompute",
    "plan",
]
