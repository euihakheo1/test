"""Parser output types: cells with source locators, tables, parsed documents.

Every cell keeps the verbatim text (used as ``SourceSpan.excerpt``) and a locator:

- CSV: ``{"row": 3, "col": 2, "col_letter": "B", "line": 3, "char_start": 41, "char_end": 49}``
  (char offsets into the decoded text, which is the document text);
- XLSX: ``{"sheet": "Sheet1", "row": 12, "col": 4, "col_letter": "D", "cell": "D12"}`` plus
  ``"merged": "A1:C1"`` for a value copied from a merged range and ``"formula": True`` for a
  cached formula result (formulas are never evaluated);
- PDF: ``{"page": 1, "table": 0, "row": 2, "col": 1, "bbox": [x0, top, x1, bottom],
  "char_start": 10, "char_end": 18}`` (char offsets into that page's text);
- HTML table (bank ".xls" files that are really HTML): ``{"table": 0, "row": 2, "col": 3}``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from jettae.domain.errors import JettaeError
from jettae.domain.status import DocumentStatus

RawValue = str | int | Decimal | date | datetime | bool | None


class SourceKind(StrEnum):
    CSV = "csv"
    XLSX = "xlsx"
    XLS_BIFF = "xls"  # legacy binary Excel (not parsed; see PROGRESS)
    HTML = "html"  # HTML table saved with an .xls extension
    PDF = "pdf"
    UNKNOWN = "unknown"


def col_letter(index: int) -> str:
    """1-based column index -> Excel letters (1 -> A, 27 -> AA)."""
    if index < 1:
        raise ValueError("column index is 1-based")
    s = ""
    n = index
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


@dataclass(frozen=True)
class Cell:
    text: str  # verbatim display text (excerpt)
    locator: Mapping[str, Any]
    raw: RawValue = None  # typed value from the file (xlsx numbers/dates); never float

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass(frozen=True)
class Row:
    index: int  # 1-based row number in the source (sheet row / CSV record / table row)
    cells: tuple[Cell, ...]

    def texts(self) -> list[str]:
        return [c.text for c in self.cells]

    @property
    def is_empty(self) -> bool:
        return all(c.is_empty for c in self.cells)

    def cell(self, col: int) -> Cell | None:
        """0-based column access; ``None`` when the row is shorter."""
        return self.cells[col] if 0 <= col < len(self.cells) else None


@dataclass(frozen=True)
class Table:
    name: str  # sheet name, "csv", "p1-t0", "html-t0"
    rows: tuple[Row, ...]
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDoc:
    source_kind: SourceKind
    status: DocumentStatus
    tables: list[Table] = field(default_factory=list)
    text: str = ""  # canonical document text; every cell excerpt occurs in it
    filename: str = ""
    encoding: str | None = None
    delimiter: str | None = None
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None  # why status is not PARSED

    @property
    def ok(self) -> bool:
        return self.status in (DocumentStatus.PARSED, DocumentStatus.NEEDS_MAPPING)


class IngestError(JettaeError):
    """Base class for ingestion failures. ``status`` says how the document is recorded."""

    status: DocumentStatus = DocumentStatus.FAILED

    def __init__(self, message: str, *, code: str = "ingest_error") -> None:
        super().__init__(message)
        self.code = code


class IngestRejected(IngestError):
    """The file violates a security policy or limit (size, rows, macros, zip bomb)."""

    status = DocumentStatus.FAILED


class CorruptFile(IngestError):
    """The file cannot be decoded/parsed. Distinct from 'no transactions'."""

    status = DocumentStatus.CORRUPT
