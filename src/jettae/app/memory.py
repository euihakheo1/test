"""In-memory adapters for the app ports (tests, demos). Thread-safe, tenant-scoped."""

from __future__ import annotations

import builtins
import hashlib
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from jettae.app.ports import STORED_ENTITIES, DecisionRecord, DocumentHead, Repositories
from jettae.domain.errors import TenantMismatchError
from jettae.domain.models import Approval, Decision, DocumentVersion, Fact
from jettae.evidence.engine import AnalysisResult
from jettae.evidence.snapshot import AnalysisConfig


class MemDocuments:
    def __init__(self) -> None:
        self._docs: dict[tuple[str, str], DocumentVersion] = {}
        self._text: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()

    def add(self, doc: DocumentVersion, text: str | None = None) -> None:
        with self._lock:
            self._docs[(doc.tenant_id, doc.id)] = doc
            if text is not None:
                self._text[(doc.tenant_id, doc.id)] = text

    def get(self, tenant_id: str, doc_version_id: str) -> DocumentVersion | None:
        return self._docs.get((tenant_id, doc_version_id))

    def latest(self, tenant_id: str, document_id: str) -> DocumentVersion | None:
        vs = [
            d for (t, _), d in self._docs.items() if t == tenant_id and d.document_id == document_id
        ]
        return max(vs, key=lambda d: d.version) if vs else None

    def list(self, tenant_id: str) -> list[DocumentVersion]:
        return sorted((d for (t, _), d in self._docs.items() if t == tenant_id), key=lambda d: d.id)

    def get_text(self, tenant_id: str, doc_version_id: str) -> str | None:
        return self._text.get((tenant_id, doc_version_id))

    def versions(self, tenant_id: str, document_id: str) -> builtins.list[DocumentVersion]:
        with self._lock:
            vs = [
                d
                for (t, _), d in self._docs.items()
                if t == tenant_id and d.document_id == document_id
            ]
        return sorted(vs, key=lambda d: d.version)


class MemBlobs:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}

    def put(self, tenant_id: str, content: bytes) -> str:
        key = hashlib.sha256(content).hexdigest()
        self._data[(tenant_id, key)] = content
        return key

    def get(self, tenant_id: str, key: str) -> bytes | None:
        return self._data.get((tenant_id, key))


class MemFacts:
    def __init__(self) -> None:
        self._facts: dict[tuple[str, str], Fact] = {}

    def add_many(self, facts: Sequence[Fact]) -> None:
        for f in facts:
            self.upsert(f)

    def upsert(self, fact: Fact) -> None:
        self._facts[(fact.tenant_id, fact.id)] = fact

    def remove(self, tenant_id: str, fact_id: str) -> None:
        self._facts.pop((tenant_id, fact_id), None)

    def get(self, tenant_id: str, fact_id: str) -> Fact | None:
        return self._facts.get((tenant_id, fact_id))

    def list(self, tenant_id: str) -> list[Fact]:
        return sorted(
            (f for (t, _), f in self._facts.items() if t == tenant_id), key=lambda f: f.id
        )

    def for_document(self, tenant_id: str, doc_version_id: str) -> builtins.list[Fact]:
        return [
            f
            for f in self.list(tenant_id)
            if f.span is not None and f.span.doc_version_id == doc_version_id
        ]


class MemLedger:
    def __init__(self) -> None:
        self._recs: dict[tuple[str, str, str], Any] = {}

    def upsert(self, entity: str, record: Any) -> None:
        if entity not in STORED_ENTITIES:
            raise ValueError(f"unknown ledger entity {entity}")
        self._recs[(record.tenant_id, entity, record.id)] = record

    def remove(self, tenant_id: str, entity: str, record_id: str) -> None:
        self._recs.pop((tenant_id, entity, record_id), None)

    def get(self, tenant_id: str, entity: str, record_id: str) -> Any | None:
        return self._recs.get((tenant_id, entity, record_id))

    def list(self, tenant_id: str, entity: str) -> list[Any]:
        return sorted(
            (r for (t, e, _), r in self._recs.items() if t == tenant_id and e == entity),
            key=lambda r: r.id,
        )


class MemDecisions:
    def __init__(self) -> None:
        self._current: dict[str, dict[str, Decision]] = {}
        self._history: dict[tuple[str, str], list[DecisionRecord]] = {}
        self._superseded: set[tuple[str, str]] = set()

    def save_current(
        self, tenant_id: str, decisions: Mapping[str, Decision], recorded_at: datetime
    ) -> None:
        for d in decisions.values():
            if d.tenant_id != tenant_id:
                raise TenantMismatchError(d.id)
        old = self._current.get(tenant_id, {})
        for did, dec in decisions.items():
            hist = self._history.setdefault((tenant_id, did), [])
            if not hist or hist[-1].decision.result_hash != dec.result_hash or hist[-1].superseded:
                hist.append(DecisionRecord(dec, recorded_at))
            self._superseded.discard((tenant_id, did))
        for did in set(old) - set(decisions):
            self._superseded.add((tenant_id, did))
            self._history.setdefault((tenant_id, did), []).append(
                DecisionRecord(old[did], recorded_at, superseded=True)
            )
        self._current[tenant_id] = dict(decisions)

    def current(self, tenant_id: str) -> dict[str, Decision]:
        return dict(self._current.get(tenant_id, {}))

    def get_current(self, tenant_id: str, decision_id: str) -> Decision | None:
        return self._current.get(tenant_id, {}).get(decision_id)

    def history(self, tenant_id: str, decision_id: str) -> list[DecisionRecord]:
        return list(self._history.get((tenant_id, decision_id), []))

    def is_superseded(self, tenant_id: str, decision_id: str) -> bool:
        return (tenant_id, decision_id) in self._superseded


class MemApprovals:
    def __init__(self) -> None:
        self._items: list[Approval] = []

    def add(self, approval: Approval) -> None:
        self._items.append(approval)

    def list_for(self, tenant_id: str, decision_id: str) -> list[Approval]:
        return [a for a in self._items if a.tenant_id == tenant_id and a.decision_id == decision_id]


class MemResults:
    def __init__(self) -> None:
        self._data: dict[str, tuple[AnalysisResult, AnalysisConfig]] = {}

    def save(self, tenant_id: str, result: AnalysisResult, config: AnalysisConfig) -> None:
        self._data[tenant_id] = (result, config)

    def load(self, tenant_id: str) -> tuple[AnalysisResult, AnalysisConfig] | None:
        return self._data.get(tenant_id)


class MemDocumentHeads:
    def __init__(self) -> None:
        self._heads: dict[tuple[str, str], DocumentHead] = {}

    def get(self, tenant_id: str, document_id: str) -> DocumentHead | None:
        return self._heads.get((tenant_id, document_id))

    def put(self, head: DocumentHead) -> None:
        self._heads[(head.tenant_id, head.document_id)] = head


class MemUnitOfWork:
    def __init__(self) -> None:
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    @contextmanager
    def transaction(self, tenant_id: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(tenant_id, threading.RLock())
        with lock:
            yield


def in_memory_repositories() -> Repositories:
    return Repositories(
        documents=MemDocuments(),
        blobs=MemBlobs(),
        facts=MemFacts(),
        ledger=MemLedger(),
        decisions=MemDecisions(),
        approvals=MemApprovals(),
        results=MemResults(),
        uow=MemUnitOfWork(),
        heads=MemDocumentHeads(),
    )
