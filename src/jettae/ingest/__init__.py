"""File ingestion: CSV/XLSX/text-PDF (+ HTML-as-xls) → rows with source locators → format
recognition and column mapping → domain records + facts with :class:`SourceSpan`.

Public entry points: :func:`parse_bytes`/:func:`safe_parse` (rows only), :func:`analyze` +
:func:`build_records` (records), :func:`ingest_document` (through ``JettaeService``).
"""

from jettae.ingest.cells import (
    Cell,
    CorruptFile,
    IngestError,
    IngestRejected,
    ParsedDoc,
    Row,
    SourceKind,
    Table,
)
from jettae.ingest.detect import parse_bytes, safe_parse, sniff_kind
from jettae.ingest.formats import FORMATS, FORMATS_BY_ID, recognize_table
from jettae.ingest.formats.base import IngestOptions, RowIssue, TotalCheck
from jettae.ingest.mapping import (
    ColumnMatch,
    Confidence,
    FieldSpec,
    MappingSuggester,
    MappingSuggestion,
    confirm_mapping,
    suggest_mapping,
)
from jettae.ingest.ocrhook import OcrCell, OcrFailed, OcrPage, OcrProvider
from jettae.ingest.pipeline import (
    IngestPlan,
    IngestResult,
    analyze,
    build_records,
    ingest_bytes,
    ingest_document,
)
from jettae.ingest.security import DEFAULT_LIMITS, Limits, escape_formula, strip_formula_prefix

__all__ = [
    "DEFAULT_LIMITS",
    "FORMATS",
    "FORMATS_BY_ID",
    "Cell",
    "ColumnMatch",
    "Confidence",
    "CorruptFile",
    "FieldSpec",
    "IngestError",
    "IngestOptions",
    "IngestPlan",
    "IngestRejected",
    "IngestResult",
    "Limits",
    "MappingSuggester",
    "MappingSuggestion",
    "OcrCell",
    "OcrFailed",
    "OcrPage",
    "OcrProvider",
    "ParsedDoc",
    "Row",
    "RowIssue",
    "SourceKind",
    "Table",
    "TotalCheck",
    "analyze",
    "build_records",
    "confirm_mapping",
    "escape_formula",
    "ingest_bytes",
    "ingest_document",
    "parse_bytes",
    "recognize_table",
    "safe_parse",
    "sniff_kind",
    "strip_formula_prefix",
    "suggest_mapping",
]
