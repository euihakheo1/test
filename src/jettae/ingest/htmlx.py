"""HTML tables saved with an ``.xls`` extension (common for Korean internet-banking exports).

Only ``<table>`` content is read; scripts, styles and links are ignored. colspan/rowspan are
expanded and flagged in the locator like merged cells.
"""

from __future__ import annotations

import re
from typing import Any

from lxml import etree
from lxml import html as lhtml

from jettae.domain.status import DocumentStatus
from jettae.ingest.cells import (
    Cell,
    CorruptFile,
    IngestRejected,
    ParsedDoc,
    Row,
    SourceKind,
    Table,
    col_letter,
)
from jettae.ingest.csvx import decode_text
from jettae.ingest.security import DEFAULT_LIMITS, Limits, check_size

_WS = re.compile(r"\s+")
_META_CHARSET = re.compile(rb"charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", re.I)


def looks_like_html(content: bytes) -> bool:
    head = content[:2048].lstrip().lower()
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:].lstrip()
    return head.startswith((b"<!doctype html", b"<html", b"<table", b"<meta", b"<head")) or (
        b"<table" in head and b"<tr" in content[:65536].lower()
    )


def _decode(content: bytes) -> tuple[str, str]:
    m = _META_CHARSET.search(content[:4096])
    if m:
        enc = m.group(1).decode("ascii").lower().replace("-", "_")
        if enc in ("euc_kr", "ks_c_5601_1987", "ksc5601"):
            enc = "cp949"
        try:
            return content.decode(enc), enc
        except (LookupError, UnicodeDecodeError):
            pass
    return decode_text(content)


def parse_html_tables(
    content: bytes, *, filename: str = "", limits: Limits = DEFAULT_LIMITS
) -> ParsedDoc:
    check_size(content, limits)
    text, enc = _decode(content)
    try:
        root = lhtml.document_fromstring(text)
    except (etree.ParserError, ValueError) as e:
        raise CorruptFile(f"cannot parse HTML table file: {e}", code="bad_html") from e
    tables: list[Table] = []
    lines: list[str] = []
    total = 0
    for t_idx, tbl in enumerate(root.iter("table")):
        grid: dict[tuple[int, int], tuple[str, dict[str, Any]]] = {}
        r_idx = 0
        for tr in tbl.iter("tr"):
            # skip rows of nested tables (they are read as their own table)
            parent = tr.getparent()
            while parent is not None and parent.tag != "table":
                parent = parent.getparent()
            if parent is not tbl:
                continue
            r_idx += 1
            c_idx = 0
            for td in tr:
                if td.tag not in ("td", "th"):
                    continue
                c_idx += 1
                while (r_idx, c_idx) in grid:
                    c_idx += 1
                val = _WS.sub(" ", td.text_content()).strip()
                try:
                    cs = max(1, min(int(td.get("colspan", "1")), limits.max_cols))
                    rs = max(1, min(int(td.get("rowspan", "1")), 1000))
                except ValueError:
                    cs = rs = 1
                span = f"{col_letter(c_idx)}{r_idx}:{col_letter(c_idx + cs - 1)}{r_idx + rs - 1}"
                for dr in range(rs):
                    for dc in range(cs):
                        loc: dict[str, Any] = {
                            "table": t_idx,
                            "row": r_idx + dr,
                            "col": c_idx + dc,
                            "col_letter": col_letter(c_idx + dc),
                        }
                        if cs > 1 or rs > 1:
                            loc["merged"] = span
                        grid[(r_idx + dr, c_idx + dc)] = (val, loc)
                c_idx += cs - 1
        if not grid:
            continue
        n_rows = max(r for r, _ in grid)
        n_cols = max(c for _, c in grid)
        total += n_rows
        if total > limits.max_rows:
            raise IngestRejected(f"rows exceed limit {limits.max_rows}", code="rows")
        if n_cols > limits.max_cols:
            raise IngestRejected("too many columns", code="cols")
        rows: list[Row] = []
        for r in range(1, n_rows + 1):
            cells = []
            for c in range(1, n_cols + 1):
                v, loc = grid.get(
                    (r, c), ("", {"table": t_idx, "row": r, "col": c, "col_letter": col_letter(c)})
                )
                cells.append(Cell(v, loc))
            rows.append(Row(r, tuple(cells)))
        tables.append(Table(f"html-t{t_idx}", tuple(rows), {"table": t_idx}))
        lines.append(f"[html-t{t_idx}]")
        lines.extend("\t".join(r.texts()) for r in rows)
    return ParsedDoc(
        source_kind=SourceKind.HTML,
        status=DocumentStatus.PARSED,
        tables=tables,
        text="\n".join(lines),
        filename=filename,
        encoding=enc,
        warnings=[] if tables else ["no <table> found"],
    )
