"""Text-PDF parsing with pdfplumber: words → page text with char offsets, ruled tables with
cell bboxes; scanned-page detection (→ ``UNSUPPORTED_SCAN`` unless an OCR provider is given).

Page text is rebuilt from pdfplumber words (lines grouped by ``top``), so every word and
table cell gets ``char_start``/``char_end`` into that page's text. The document text is the
page texts joined by ``"\\n\\n"``; ``doc_char_start`` gives the offset in it.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError
from pdfminer.psparser import PSException

from jettae.domain.status import DocumentStatus
from jettae.ingest.cells import (
    Cell,
    CorruptFile,
    IngestRejected,
    ParsedDoc,
    Row,
    SourceKind,
    Table,
)
from jettae.ingest.ocrhook import OcrFailed, OcrPage, OcrProvider
from jettae.ingest.security import DEFAULT_LIMITS, Limits, check_size

_LINE_TOL = 3.0  # points: words whose tops differ less than this are on one line
_MIN_TEXT_CHARS = 1
_TEXT_TABLE = {"vertical_strategy": "text", "horizontal_strategy": "text"}

logging.getLogger("pdfminer").setLevel(logging.ERROR)


@dataclass(frozen=True)
class _Word:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    start: int
    end: int


def _page_text(words: list[dict[str, Any]]) -> tuple[str, list[_Word]]:
    ws = sorted(words, key=lambda w: (float(w["top"]), float(w["x0"])))
    lines: list[list[dict[str, Any]]] = []
    for w in ws:
        if lines and abs(float(w["top"]) - float(lines[-1][0]["top"])) <= _LINE_TOL:
            lines[-1].append(w)
        else:
            lines.append([w])
    parts: list[str] = []
    out: list[_Word] = []
    pos = 0
    for li, line in enumerate(lines):
        if li:
            parts.append("\n")
            pos += 1
        for wi, w in enumerate(sorted(line, key=lambda w: float(w["x0"]))):
            if wi:
                parts.append(" ")
                pos += 1
            # NUL is not storable as PostgreSQL text; replace 1:1 so offsets stay valid
            t = str(w["text"]).replace("\x00", "\ufffd")
            out.append(
                _Word(
                    t,
                    float(w["x0"]),
                    float(w["top"]),
                    float(w["x1"]),
                    float(w["bottom"]),
                    pos,
                    pos + len(t),
                )
            )
            parts.append(t)
            pos += len(t)
    return "".join(parts), out


def _bbox(b: Sequence[float]) -> list[int]:
    """Integer bbox (rounded points) so locators stay float-free for canonical hashing."""
    return [round(float(v)) for v in b]


def _cell_range(words: list[_Word], bbox: Sequence[float]) -> tuple[int, int] | None:
    x0, top, x1, bottom = (float(v) for v in bbox)
    inside = [
        w
        for w in words
        if x0 - 0.5 <= (w.x0 + w.x1) / 2 <= x1 + 0.5
        and top - 0.5 <= (w.top + w.bottom) / 2 <= bottom + 0.5
    ]
    if not inside:
        return None
    return min(w.start for w in inside), max(w.end for w in inside)


def _tables_for_page(
    page: Any, page_no: int, words: list[_Word], doc_offset: int, limits: Limits
) -> list[Table]:
    out: list[Table] = []
    found = page.find_tables()
    strategy = "lines"
    if not found and words:
        found = [t for t in page.find_tables(_TEXT_TABLE) if len(t.rows) >= 2]
        strategy = "text"
    for t_idx, tbl in enumerate(found):
        data = tbl.extract()
        rows: list[Row] = []
        for r_idx, prow in enumerate(tbl.rows):
            texts = data[r_idx] if r_idx < len(data) else []
            if len(prow.cells) > limits.max_cols:
                raise IngestRejected("too many columns in PDF table", code="cols")
            cells: list[Cell] = []
            for c_idx, cb in enumerate(prow.cells):
                txt = (texts[c_idx] if c_idx < len(texts) else None) or ""
                txt = " ".join(txt.split())
                loc: dict[str, Any] = {
                    "page": page_no,
                    "table": t_idx,
                    "row": r_idx + 1,
                    "col": c_idx + 1,
                    "strategy": strategy,
                }
                if cb is not None:
                    loc["bbox"] = _bbox(cb)
                    rng = _cell_range(words, cb)
                    if rng is not None:
                        loc["char_start"], loc["char_end"] = rng
                        loc["doc_char_start"] = doc_offset + rng[0]
                cells.append(Cell(txt, loc))
            rows.append(Row(r_idx + 1, tuple(cells)))
        out.append(
            Table(f"p{page_no}-t{t_idx}", tuple(rows), {"page": page_no, "strategy": strategy})
        )
    return out


def _ocr_tables(pages: Sequence[OcrPage]) -> list[Table]:
    tables: list[Table] = []
    for op in pages:
        for t_idx, tbl in enumerate(op.tables):
            rows: list[Row] = []
            for r_idx, orow in enumerate(tbl, start=1):
                cells = []
                for c_idx, oc in enumerate(orow, start=1):
                    loc: dict[str, Any] = {
                        "page": op.page,
                        "table": t_idx,
                        "row": r_idx,
                        "col": c_idx,
                        "ocr": op.provider or "ocr",
                    }
                    if oc.bbox is not None:
                        loc["bbox"] = list(oc.bbox)
                    cells.append(Cell(oc.text, loc))
                rows.append(Row(r_idx, tuple(cells)))
            tables.append(
                Table(f"p{op.page}-ocr{t_idx}", tuple(rows), {"page": op.page, "ocr": True})
            )
    return tables


def parse_pdf(
    content: bytes,
    *,
    filename: str = "",
    limits: Limits = DEFAULT_LIMITS,
    ocr: OcrProvider | None = None,
) -> ParsedDoc:
    check_size(content, limits)
    if not content.lstrip()[:5] == b"%PDF-":
        raise CorruptFile("not a PDF (missing %PDF- header)", code="bad_pdf")
    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except (PDFPasswordIncorrect, PDFEncryptionError) as e:
        raise IngestRejected("encrypted PDF: remove the password first", code="encrypted") from e
    except (PDFSyntaxError, PSException) as e:
        raise CorruptFile(f"cannot read PDF: {e}", code="bad_pdf") from e
    except Exception as e:  # pdfminer raises a variety of types on damaged files
        if "password" in str(e).lower() or "encrypt" in type(e).__name__.lower():
            raise IngestRejected(
                "encrypted PDF: remove the password first", code="encrypted"
            ) from e
        raise CorruptFile(f"cannot read PDF: {type(e).__name__}: {e}", code="bad_pdf") from e
    warnings: list[str] = []
    page_meta: list[dict[str, Any]] = []
    tables: list[Table] = []
    texts: list[str] = []
    offset = 0
    try:
        with pdf:
            if len(pdf.pages) > limits.max_pages:
                raise IngestRejected(f"{len(pdf.pages)} pages exceed limit", code="pages")
            n_rows = 0
            for p_idx, page in enumerate(pdf.pages, start=1):
                words_raw = page.extract_words(keep_blank_chars=False)
                text, words = _page_text(words_raw)
                n_img = len(page.images)
                if len(text.strip()) >= _MIN_TEXT_CHARS:
                    kind = "text"
                elif n_img:
                    kind = "image_only"
                else:
                    kind = "blank"
                page_meta.append({"page": p_idx, "kind": kind, "chars": len(text), "images": n_img})
                if kind == "text":
                    pts = _tables_for_page(page, p_idx, words, offset, limits)
                    n_rows += sum(len(t.rows) for t in pts)
                    if n_rows > limits.max_rows:
                        raise IngestRejected("rows exceed limit", code="rows")
                    tables.extend(pts)
                texts.append(text)
                offset += len(text) + 2
    except (IngestRejected, CorruptFile):
        raise
    except Exception as e:
        raise CorruptFile(
            f"cannot read PDF page content: {type(e).__name__}: {e}", code="bad_pdf"
        ) from e

    doc = ParsedDoc(
        source_kind=SourceKind.PDF,
        status=DocumentStatus.PARSED,
        tables=tables,
        text="\n\n".join(texts),
        filename=filename,
        warnings=warnings,
        meta={"pages": page_meta},
    )
    scanned = [m["page"] for m in page_meta if m["kind"] == "image_only"]
    text_pages = [m["page"] for m in page_meta if m["kind"] == "text"]
    if scanned:
        if ocr is not None:
            try:
                pages = ocr.ocr_pdf_pages(content, scanned)
            except OcrFailed:
                raise
            except Exception as e:  # provider bug/timeout: not "no rows"
                raise OcrFailed(f"OCR provider {getattr(ocr, 'name', '?')} failed: {e}") from e
            doc.tables.extend(_ocr_tables(pages))
            ocr_text = "\n\n".join(p.text for p in pages)
            doc.text = (doc.text + "\n\n" + ocr_text) if doc.text else ocr_text
            doc.meta["ocr"] = {"provider": getattr(ocr, "name", "ocr"), "pages": scanned}
            doc.warnings.append("scanned pages transcribed by OCR (unverified transcription)")
        elif not text_pages:
            doc.status = DocumentStatus.UNSUPPORTED_SCAN
            doc.reason = "scanned PDF (no text layer) and no OCR provider configured"
        else:
            doc.meta["unread_pages"] = scanned
            doc.reason = "partial_scan"
            doc.warnings.append(
                f"pages {scanned} have no text layer and were not read (no OCR provider)"
            )
    if not page_meta or all(m["kind"] == "blank" for m in page_meta):
        doc.warnings.append("PDF has no text and no images")
    return doc
