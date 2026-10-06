"""Bridge from the API / worker to the ingestion package (loaded lazily on first use).

Interface: the entrypoint module (default ``jettae.ingest.pipeline``, override with
``JETTAE_INGEST_ENTRYPOINT``) provides::

    def parse_document(content: bytes, *, filename: str, media_type: str, kind: str,
                       tenant_id: str, doc_version_id: str,
                       mapping: Mapping[str, Any] | None,     # MappingRequest JSON
                       document_key: str = ..., observed_at: datetime = ...) -> ParseResult
    def suggest_mapping(content: bytes, *, filename: str, media_type: str,
                        kind: str) -> Mapping[str, Any]      # JSON-compatible

The result is validated into :class:`jettae.app.contracts.ParseResult` (status, facts,
records, text, reason, suggestion, notes, row ``issues``, table ``totals`` and row
``counts``). Nothing is dropped on the way: row-level issues and total checks reach the job
result and the document detail. Every fact/record must carry the given ``tenant_id``
(checked again by the service).

If the module or a function is missing, :class:`IngestUnavailable` is raised with an
explicit message; the API answers 503 ``ingest_unavailable`` and ingest jobs fail
permanently with that message.
"""

from __future__ import annotations

import importlib
import inspect
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from pydantic import ValidationError

from jettae.app.contracts import MappingRequest, ParseResult
from jettae.domain.models import DocumentVersion

DEFAULT_ENTRYPOINT = "jettae.ingest.pipeline"

# The bridge result is the strict contract model (old name kept for imports).
ParsedDocument = ParseResult


class IngestUnavailable(RuntimeError):
    code = "ingest_unavailable"


class IngestContractError(RuntimeError):
    """The parser returned something that does not satisfy :class:`ParseResult`."""

    code = "ingest_contract_error"


class IngestPort(Protocol):
    def parse(
        self, doc: DocumentVersion, content: bytes, mapping: MappingRequest | None
    ) -> ParseResult: ...

    def suggest_mapping(self, doc: DocumentVersion, content: bytes) -> Mapping[str, Any]: ...


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def normalize_result(raw: Any) -> ParseResult:
    """Validate a parser result into :class:`ParseResult` (issues/totals/counts included)."""
    if isinstance(raw, ParseResult):
        return raw
    if _get(raw, "status") is None:
        raise IngestContractError("ingest result has no status")
    sugg = _get(raw, "suggestion")
    if sugg is not None and hasattr(sugg, "as_dict"):
        sugg = sugg.as_dict()
    data = {
        "status": _get(raw, "status"),
        "facts": tuple(_get(raw, "facts", ()) or ()),
        "records": tuple(_get(raw, "records", ()) or ()),
        "text": _get(raw, "text"),
        "reason": _get(raw, "reason"),
        "suggestion": sugg,
        "notes": tuple(str(n) for n in (_get(raw, "notes") or _get(raw, "warnings") or ())),
        "issues": tuple(_get(raw, "issues", ()) or ()),
        "totals": tuple(_get(raw, "totals", ()) or ()),
        "counts": _get(raw, "counts"),
    }
    try:
        return ParseResult.model_validate(data)
    except ValidationError as e:
        raise IngestContractError(f"ingest result violates the parse contract: {e}") from None


def _accepts(fn: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def _ocr_provider(tenant_id: str) -> Any:
    """Scanned-PDF OCR provider from the environment (``JETTAE_OCR_PROVIDER=vlm``), else
    None (scans then end as UNSUPPORTED_SCAN). Never raises: OCR is optional."""
    if os.environ.get("JETTAE_OCR_PROVIDER", "").lower() != "vlm":
        return None
    from jettae.ocr.vlm import pdf_ocr_from_env

    return pdf_ocr_from_env(tenant_id)


class ModuleIngest:
    """Loads the entrypoint module on first use (never at import time)."""

    def __init__(self, entrypoint: str = DEFAULT_ENTRYPOINT) -> None:
        self.entrypoint = entrypoint

    def _fn(self, name: str) -> Callable[..., Any]:
        try:
            mod = importlib.import_module(self.entrypoint)
        except ModuleNotFoundError as e:
            raise IngestUnavailable(
                f"ingestion module {self.entrypoint!r} is not installed ({e.name} missing); "
                "documents are stored but cannot be parsed yet"
            ) from None
        fn = getattr(mod, name, None)
        if not callable(fn):
            raise IngestUnavailable(
                f"ingestion module {self.entrypoint!r} does not provide {name}() "
                "(see jettae.db.ingest_bridge for the expected contract)"
            )
        return fn

    def parse(
        self, doc: DocumentVersion, content: bytes, mapping: MappingRequest | None
    ) -> ParseResult:
        fn = self._fn("parse_document")
        extra: dict[str, Any] = {}
        # Record ids must be keyed by the *document* (not the version) so that a corrected
        # version updates kept rows instead of adding a second copy beside them.
        if _accepts(fn, "document_key"):
            extra["document_key"] = doc.document_id
        # Facts observed "when the version was registered": re-parsing the same version
        # yields identical facts, so an unchanged re-run is detected by its fingerprint.
        if _accepts(fn, "observed_at"):
            extra["observed_at"] = doc.created_at
        if _accepts(fn, "ocr"):
            ocr = _ocr_provider(doc.tenant_id)
            if ocr is not None:
                extra["ocr"] = ocr
        raw = fn(
            content,
            filename=doc.filename,
            media_type=doc.media_type,
            kind=doc.kind.value,
            tenant_id=doc.tenant_id,
            doc_version_id=doc.id,
            mapping=mapping.model_dump(mode="json") if mapping is not None else None,
            **extra,
        )
        return normalize_result(raw)

    def suggest_mapping(self, doc: DocumentVersion, content: bytes) -> Mapping[str, Any]:
        fn = self._fn("suggest_mapping")
        out = fn(content, filename=doc.filename, media_type=doc.media_type, kind=doc.kind.value)
        if hasattr(out, "as_dict"):
            out = out.as_dict()
        if not isinstance(out, Mapping):
            raise TypeError("suggest_mapping must return a mapping")
        return out
