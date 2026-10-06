"""Invalidation planner: which decisions must be recomputed after a set of changes.

Sources of invalidation:
1. direct edges: a changed fact / document version / record reaches decisions through the
   dependency graph;
2. query scopes: every scope recorded in the graph is re-evaluated on the new snapshot and
   compared with the hash observed at computation time (catches inserted records, e.g. an
   agreement added after a decision relied on "no agreement found");
3. new subjects (no decision yet) and removed subjects.

Anything the planner cannot reason about -> ``fallback_full=True`` (full recompute):
untracked entity kinds, decisions without tracked dependencies, unknown scope kinds,
a graph that does not cover the existing decisions, or no new snapshot to evaluate scopes.
"""

from __future__ import annotations

from collections.abc import Iterable

from jettae.domain.models import Change, RecomputePlan
from jettae.evidence.deps import DependencyGraph, node_doc, node_fact, node_rec
from jettae.evidence.snapshot import Snapshot, UnknownScopeError, decision_id_for

RECORD_ENTITIES = frozenset(
    {"invoice", "settlement_line", "bank_txn", "agreement", "evidence_link"}
)
TRACKED_ENTITIES = RECORD_ENTITIES | {"fact", "doc_version", "config"}


def _node_for(change: Change) -> str | None:
    if change.entity == "fact":
        return node_fact(change.entity_id)
    if change.entity == "doc_version":
        return node_doc(change.entity_id)
    if change.entity in RECORD_ENTITIES:
        return node_rec(change.entity, change.entity_id)
    return None


def _full(reason: str, decision_ids: Iterable[str]) -> RecomputePlan:
    ids = frozenset(decision_ids)
    return RecomputePlan(
        affected=ids,
        reasons={d: (reason,) for d in ids},
        fallback_full=True,
        notes=(f"전체 재계산: {reason}",),
    )


def plan(
    graph: DependencyGraph,
    changes: Iterable[Change],
    snapshot: Snapshot | None = None,
    *,
    existing_decisions: Iterable[str] | None = None,
) -> RecomputePlan:
    """Plan recomputation for ``changes`` (already applied to ``snapshot``)."""
    changes = list(changes)
    existing = (
        frozenset(existing_decisions) if existing_decisions is not None else (graph.decision_ids)
    )
    all_ids = set(existing)
    if snapshot is not None:
        all_ids |= {decision_id_for(s) for s in snapshot.receivables}

    for c in changes:
        if c.entity not in TRACKED_ENTITIES:
            return _full(f"추적하지 않는 변경 유형 '{c.entity}'", all_ids)
    missing = sorted(existing - graph.decision_ids)
    if missing:
        return _full(f"의존성 그래프에 없는 결과 {missing[:3]}", all_ids)
    untracked = sorted(d for d in existing if not graph.is_tracked(d))
    if untracked:
        return _full(f"의존성이 추적되지 않은 결과 {untracked[:3]}", all_ids)
    if snapshot is None:
        return _full("새 스냅샷 없이 조회 범위를 평가할 수 없음", all_ids)

    reasons: dict[str, list[str]] = {}

    def hit(dids: Iterable[str], why: str) -> None:
        for d in dids:
            if d in existing:
                reasons.setdefault(d, [])
                if why not in reasons[d]:
                    reasons[d].append(why)

    # 1. direct edges
    for c in changes:
        node = _node_for(c)
        if node is not None:
            hit(graph.dependents(node), f"{c.entity} {c.entity_id} {c.kind.value}")

    # 2. query scopes (re-evaluated on the new snapshot)
    for scope, observed in sorted(graph.all_scopes().items()):
        try:
            new_hash = snapshot.evaluate_scope(scope)
        except UnknownScopeError:
            return _full(f"알 수 없는 조회 범위 {scope}", all_ids)
        changed = [d for d, h in observed.items() if h != new_hash]
        if changed:
            hit(changed, f"조회 범위 {scope} 결과 변경")

    # 3. new / removed subjects
    new_subjects = {decision_id_for(s) for s in snapshot.receivables}
    removed = frozenset(existing - new_subjects)
    for d in sorted(new_subjects - existing):
        reasons.setdefault(d, []).append("새 거래 항목")
    for d in removed:
        reasons.pop(d, None)

    return RecomputePlan(
        affected=frozenset(reasons),
        reasons={d: tuple(r) for d, r in sorted(reasons.items())},
        fallback_full=False,
        removed=removed,
    )
