"""File-type detection by content (magic bytes), not by extension, and parser dispatch."""

from __future__ import annotations

import io
import zipfile

from jettae.domain.status import DocumentStatus
from jettae.ingest.cells import IngestError, ParsedDoc, SourceKind
from jettae.ingest.csvx import parse_csv
from jettae.ingest.htmlx import looks_like_html, parse_html_tables
from jettae.ingest.ocrhook import OcrProvider
from jettae.ingest.pdfx import parse_pdf
from jettae.ingest.security import DEFAULT_LIMITS, Limits, check_ole_macros, check_size
from jettae.ingest.xlsx import parse_xlsx

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MEDIA_TYPES = {
    SourceKind.CSV: "text/csv",
    SourceKind.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    SourceKind.XLS_BIFF: "application/vnd.ms-excel",
    SourceKind.HTML: "text/html",
    SourceKind.PDF: "application/pdf",
    SourceKind.UNKNOWN: "application/octet-stream",
}


def sniff_kind(content: bytes) -> SourceKind:
    head = content[:8]
    if content.lstrip()[:5] == b"%PDF-":
        return SourceKind.PDF
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            return SourceKind.XLSX  # damaged zip container: reported as CORRUPT by the parser
        if "xl/workbook.xml" in names or "xl/workbook.bin" in names:
            return SourceKind.XLSX
        return SourceKind.UNKNOWN
    if head == OLE_MAGIC:
        return SourceKind.XLS_BIFF
    if looks_like_html(content):
        return SourceKind.HTML
    if b"\x00" in content[:4096] and not content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return SourceKind.UNKNOWN
    return SourceKind.CSV


def parse_bytes(
    content: bytes,
    filename: str = "",
    *,
    limits: Limits = DEFAULT_LIMITS,
    ocr: OcrProvider | None = None,
) -> ParsedDoc:
    """Parse a file. Raises :class:`IngestError` subclasses on rejection/corruption."""
    check_size(content, limits)
    kind = sniff_kind(content)
    if kind is SourceKind.PDF:
        return parse_pdf(content, filename=filename, limits=limits, ocr=ocr)
    if kind is SourceKind.XLSX:
        return parse_xlsx(content, filename=filename, limits=limits)
    if kind is SourceKind.HTML:
        return parse_html_tables(content, filename=filename, limits=limits)
    if kind is SourceKind.CSV:
        return parse_csv(content, filename=filename, limits=limits)
    if kind is SourceKind.XLS_BIFF:
        check_ole_macros(content)
        return ParsedDoc(
            source_kind=kind,
            status=DocumentStatus.FAILED,
            filename=filename,
            reason="legacy binary .xls is not supported: save as .xlsx or CSV and upload again",
        )
    return ParsedDoc(
        source_kind=SourceKind.UNKNOWN,
        status=DocumentStatus.FAILED,
        filename=filename,
        reason="unsupported file type (expected CSV, XLSX or PDF)",
    )


def safe_parse(
    content: bytes,
    filename: str = "",
    *,
    limits: Limits = DEFAULT_LIMITS,
    ocr: OcrProvider | None = None,
) -> ParsedDoc:
    """Like :func:`parse_bytes` but never raises for bad input: the status/reason say why."""
    try:
        return parse_bytes(content, filename, limits=limits, ocr=ocr)
    except IngestError as e:
        return ParsedDoc(
            source_kind=sniff_kind(content) if content else SourceKind.UNKNOWN,
            status=e.status,
            filename=filename,
            reason=f"{e.code}: {e}",
            meta={"error_code": e.code},
        )
