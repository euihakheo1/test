"""Dependency graph: doc_version -> fact -> record -> computation -> decision, plus query scopes.

Node ids are typed strings (see ``node_*`` helpers). For each decision the graph stores
its full upstream closure (inverted index node -> decisions) and the query scopes it read,
each with the result-set hash observed at computation time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from jettae.evidence.snapshot import QueryScope


def node_doc(doc_version_id: str) -> str:
    return f"doc:{doc_version_id}"


def node_fact(fact_id: str) -> str:
    return f"fact:{fact_id}"


def node_rec(entity: str, record_id: str) -> str:
    return f"rec:{entity}:{record_id}"


def node_comp(decision_id: str, name: str) -> str:
    return f"comp:{decision_id}:{name}"


def node_dec(decision_id: str) -> str:
    return f"dec-node:{decision_id}"


class DependencyGraph:
    def __init__(self) -> None:
        self._edges: dict[str, frozenset[tuple[str, str]]] = {}
        self._upstream: dict[str, frozenset[str]] = {}
        self._index: dict[str, set[str]] = {}
        self._scopes: dict[str, dict[QueryScope, str]] = {}
        self._scope_index: dict[QueryScope, set[str]] = {}
        self._tracked: dict[str, bool] = {}

    # -- building -------------------------------------------------------------------
    def add_decision(
        self,
        decision_id: str,
        edges: Iterable[tuple[str, str]],
        scopes: Mapping[QueryScope, str],
        *,
        tracked: bool = True,
    ) -> None:
        if decision_id in self._upstream:
            self.remove_decision(decision_id)
        edge_set = frozenset(edges)
        target = node_dec(decision_id)
        rev: dict[str, set[str]] = {}
        for src, dst in edge_set:
            rev.setdefault(dst, set()).add(src)
        seen: set[str] = set()
        stack = [target]
        while stack:
            n = stack.pop()
            for src in rev.get(n, ()):
                if src not in seen:
                    seen.add(src)
                    stack.append(src)
        self._edges[decision_id] = edge_set
        self._upstream[decision_id] = frozenset(seen)
        for n in seen:
            self._index.setdefault(n, set()).add(decision_id)
        self._scopes[decision_id] = dict(scopes)
        for s in scopes:
            self._scope_index.setdefault(s, set()).add(decision_id)
        self._tracked[decision_id] = tracked

    def remove_decision(self, decision_id: str) -> None:
        for n in self._upstream.pop(decision_id, frozenset()):
            ds = self._index.get(n)
            if ds is not None:
                ds.discard(decision_id)
                if not ds:
                    del self._index[n]
        for s in self._scopes.pop(decision_id, {}):
            ds = self._scope_index.get(s)
            if ds is not None:
                ds.discard(decision_id)
                if not ds:
                    del self._scope_index[s]
        self._edges.pop(decision_id, None)
        self._tracked.pop(decision_id, None)

    # -- queries --------------------------------------------------------------------
    @property
    def decision_ids(self) -> frozenset[str]:
        return frozenset(self._upstream)

    def dependents(self, node: str) -> frozenset[str]:
        return frozenset(self._index.get(node, ()))

    def upstream(self, decision_id: str) -> frozenset[str]:
        return self._upstream.get(decision_id, frozenset())

    def edges(self, decision_id: str) -> frozenset[tuple[str, str]]:
        return self._edges.get(decision_id, frozenset())

    def scopes_of(self, decision_id: str) -> Mapping[QueryScope, str]:
        return dict(self._scopes.get(decision_id, {}))

    def all_scopes(self) -> dict[QueryScope, dict[str, str]]:
        """scope -> {decision_id: observed hash}."""
        out: dict[QueryScope, dict[str, str]] = {}
        for did, scopes in self._scopes.items():
            for s, h in scopes.items():
                out.setdefault(s, {})[did] = h
        return out

    def is_tracked(self, decision_id: str) -> bool:
        return self._tracked.get(decision_id, False)

    def copy(self) -> DependencyGraph:
        g = DependencyGraph()
        g._edges = dict(self._edges)
        g._upstream = dict(self._upstream)
        g._index = {k: set(v) for k, v in self._index.items()}
        g._scopes = {k: dict(v) for k, v in self._scopes.items()}
        g._scope_index = {k: set(v) for k, v in self._scope_index.items()}
        g._tracked = dict(self._tracked)
        return g

    # -- persistence (JSON-compatible) ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "decisions": {
                did: {
                    "edges": sorted([list(e) for e in self._edges[did]]),
                    "scopes": {str(s): h for s, h in sorted(self._scopes[did].items())},
                    "tracked": self._tracked[did],
                }
                for did in sorted(self._upstream)
            }
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DependencyGraph:
        g = cls()
        for did, d in data.get("decisions", {}).items():
            g.add_decision(
                did,
                [(e[0], e[1]) for e in d["edges"]],
                {QueryScope.parse(s): h for s, h in d["scopes"].items()},
                tracked=bool(d.get("tracked", True)),
            )
        return g

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DependencyGraph) and self.to_dict() == other.to_dict()

    __hash__ = None  # type: ignore[assignment]
