"""Repository ports (Protocols). Adapters (in-memory here, SQLAlchemy in ``jettae.db``)
implement them. Every method is tenant-scoped; adapters must never return another
tenant's objects.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from jettae.domain.models import Approval, Decision, DocumentVersion, Fact
from jettae.evidence.engine import AnalysisResult
from jettae.evidence.snapshot import AnalysisConfig

# Records derived from documents (owned by a document version through their facts; the
# document applier adds/removes exactly these).
LEDGER_ENTITIES = ("invoice", "settlement_line", "bank_txn", "agreement")
# Records written by users, not by documents: an ``EvidenceLink`` says how a tax invoice
# relates to settlement lines. They are part of the analysis snapshot but are never touched
# by document application (a link whose invoice disappears simply has no effect).
CONFIRMATION_ENTITIES = ("evidence_link",)
# Everything the LedgerRepo stores.
STORED_ENTITIES = LEDGER_ENTITIES + CONFIRMATION_ENTITIES


@runtime_checkable
class DocumentRepo(Protocol):
    def add(self, doc: DocumentVersion, text: str | None = None) -> None: ...
    def get(self, tenant_id: str, doc_version_id: str) -> DocumentVersion | None: ...
    def latest(self, tenant_id: str, document_id: str) -> DocumentVersion | None: ...
    def list(self, tenant_id: str) -> list[DocumentVersion]: ...
    def get_text(self, tenant_id: str, doc_version_id: str) -> str | None: ...
    def versions(self, tenant_id: str, document_id: str) -> builtins.list[DocumentVersion]:
        """All versions of one document, oldest first."""
        ...


@runtime_checkable
class BlobStore(Protocol):
    def put(self, tenant_id: str, content: bytes) -> str: ...  # returns storage key
    def get(self, tenant_id: str, key: str) -> bytes | None: ...


@runtime_checkable
class FactRepo(Protocol):
    def add_many(self, facts: Sequence[Fact]) -> None: ...
    def upsert(self, fact: Fact) -> None: ...
    def remove(self, tenant_id: str, fact_id: str) -> None: ...
    def get(self, tenant_id: str, fact_id: str) -> Fact | None: ...
    def list(self, tenant_id: str) -> list[Fact]: ...
    def for_document(self, tenant_id: str, doc_version_id: str) -> builtins.list[Fact]:
        """Facts whose source span points at this document version."""
        ...


@runtime_checkable
class LedgerRepo(Protocol):
    """Stores Invoice / SettlementLine / BankTxn / Agreement records and user
    EvidenceLink confirmations by entity name (see ``STORED_ENTITIES``)."""

    def upsert(self, entity: str, record: Any) -> None: ...
    def remove(self, tenant_id: str, entity: str, record_id: str) -> None: ...
    def get(self, tenant_id: str, entity: str, record_id: str) -> Any | None: ...
    def list(self, tenant_id: str, entity: str) -> list[Any]: ...


@dataclass(frozen=True)
class DecisionRecord:
    decision: Decision
    recorded_at: datetime
    superseded: bool = False  # subject no longer exists in the current snapshot


@runtime_checkable
class DecisionRepo(Protocol):
    def save_current(
        self, tenant_id: str, decisions: Mapping[str, Decision], recorded_at: datetime
    ) -> None:
        """Replace the current set. Versions whose result_hash changed are appended to the
        history; decisions missing from ``decisions`` are marked superseded."""
        ...

    def current(self, tenant_id: str) -> dict[str, Decision]: ...
    def get_current(self, tenant_id: str, decision_id: str) -> Decision | None: ...
    def history(self, tenant_id: str, decision_id: str) -> list[DecisionRecord]: ...
    def is_superseded(self, tenant_id: str, decision_id: str) -> bool: ...


@runtime_checkable
class ApprovalRepo(Protocol):
    def add(self, approval: Approval) -> None: ...
    def list_for(self, tenant_id: str, decision_id: str) -> list[Approval]: ...


@runtime_checkable
class ResultStore(Protocol):
    """Keeps the last analysis result (incl. dependency graph) and its config per tenant.
    ``load`` may return None (then the next change triggers a full recompute)."""

    def save(self, tenant_id: str, result: AnalysisResult, config: AnalysisConfig) -> None: ...
    def load(self, tenant_id: str) -> tuple[AnalysisResult, AnalysisConfig] | None: ...


@dataclass(frozen=True)
class DocumentHead:
    """The *current* version of one document: the version whose rows are in the ledger.

    Exactly one version per document is current. It only moves forward (see
    :mod:`jettae.app.doc_apply`): an older version can never become current again, a parse
    failure never moves it. ``fingerprint`` identifies the applied parse output;
    ``needs_ack`` marks an incomplete application (rows excluded, values unread, totals
    differing) and ``ack_fingerprint`` records which output a user acknowledged."""

    tenant_id: str
    document_id: str
    doc_version_id: str
    version: int
    fingerprint: str
    state: str  # jettae.app.contracts.ApplyState value
    needs_ack: bool
    applied_at: datetime
    ack_fingerprint: str | None = None
    ack_by: str | None = None
    ack_at: datetime | None = None

    @property
    def acknowledged(self) -> bool:
        return self.needs_ack and self.ack_fingerprint == self.fingerprint

    @property
    def blocks_approval(self) -> bool:
        return self.needs_ack and not self.acknowledged


@runtime_checkable
class DocumentHeadRepo(Protocol):
    """Current-version pointer per document. Read and written inside the tenant's write
    transaction (``UnitOfWork.transaction``), which serialises concurrent appliers."""

    def get(self, tenant_id: str, document_id: str) -> DocumentHead | None: ...
    def put(self, head: DocumentHead) -> None: ...


@runtime_checkable
class UnitOfWork(Protocol):
    def transaction(self, tenant_id: str) -> AbstractContextManager[None]:
        """Serialises state-changing use cases of one tenant (DB adapters: a transaction
        with row locks / SERIALIZABLE; in-memory: a per-tenant lock)."""
        ...


@dataclass
class Repositories:
    documents: DocumentRepo
    blobs: BlobStore
    facts: FactRepo
    ledger: LedgerRepo
    decisions: DecisionRepo
    approvals: ApprovalRepo
    results: ResultStore
    uow: UnitOfWork
    heads: DocumentHeadRepo | None = None  # required by jettae.app.doc_apply


def iter_ledger(repos: Repositories, tenant_id: str) -> Iterator[tuple[str, Any]]:
    for e in LEDGER_ENTITIES:
        for r in repos.ledger.list(tenant_id, e):
            yield e, r
