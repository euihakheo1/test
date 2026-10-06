"""Process wiring shared by the API and the worker: settings -> DB, blobs, repos, service,
job queue, ingest bridge."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from jettae.app.doc_apply import DocumentApplier
from jettae.app.ports import Repositories
from jettae.app.services import JettaeService
from jettae.db.config import Settings
from jettae.db.ingest_bridge import IngestPort, ModuleIngest
from jettae.db.jobs import JobQueue
from jettae.db.repos import (
    SqlDecisions,
    SqlDocumentHeads,
    SqlDocuments,
    SqlFacts,
    SqlMappings,
    sql_repositories,
)
from jettae.db.session import Database
from jettae.domain.dates import utc_now
from jettae.store.files import FileBlobStore


@dataclass
class Runtime:
    settings: Settings
    db: Database
    blobs: FileBlobStore
    repos: Repositories
    service: JettaeService
    queue: JobQueue
    ingest: IngestPort
    clock: Callable[[], datetime]
    mappings: SqlMappings

    # typed accessors for adapter-specific extras
    @property
    def documents(self) -> SqlDocuments:
        assert isinstance(self.repos.documents, SqlDocuments)
        return self.repos.documents

    @property
    def facts(self) -> SqlFacts:
        assert isinstance(self.repos.facts, SqlFacts)
        return self.repos.facts

    @property
    def decisions(self) -> SqlDecisions:
        assert isinstance(self.repos.decisions, SqlDecisions)
        return self.repos.decisions

    @property
    def heads(self) -> SqlDocumentHeads:
        assert isinstance(self.repos.heads, SqlDocumentHeads)
        return self.repos.heads

    @property
    def applier(self) -> DocumentApplier:
        """The document-application service (version promotion, zero-row and parse-failure
        rules, partial-application policy) shared with the in-process ingest path."""
        return DocumentApplier(self.service)

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        ingest: IngestPort | None = None,
        db: Database | None = None,
    ) -> Runtime:
        st = settings or Settings()
        database = db or Database(st.database_url)
        blobs = FileBlobStore(st.blob_dir)
        repos = sql_repositories(database, blobs)
        return cls(
            settings=st,
            db=database,
            blobs=blobs,
            repos=repos,
            service=JettaeService(repos, clock=clock),
            queue=JobQueue(
                database,
                clock=clock,
                lease_s=st.job_lease_s,
                max_attempts=st.job_max_attempts,
                backoff_base_s=st.job_backoff_base_s,
                backoff_max_s=st.job_backoff_max_s,
            ),
            ingest=ingest or ModuleIngest(st.ingest_entrypoint),
            clock=clock,
            mappings=SqlMappings(database),
        )

    def close(self) -> None:
        self.db.dispose()
