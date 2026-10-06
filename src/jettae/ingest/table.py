"""Header-row detection (incl. two-row headers under merged group labels), data/total rows.

Header detection scores candidate rows against the known header vocabulary (all format
synonyms). Rows above the header are kept as ``preamble`` (titles like "매출 전자세금계산서
목록", bank/account lines) for format and direction hints. Total rows (합계/총계/소계/계) and
repeated header rows are separated from data rows, never silently summed into records.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from jettae.ingest.cells import Row, Table
from jettae.ingest.mapping import normalize_header

MAX_SCAN = 40
_NUMERIC = re.compile(r"^[\s\d,.\-+()/:원₩]*$")
TOTAL_WORDS = frozenset(
    {
        "합계",
        "총계",
        "소계",
        "계",
        "누계",
        "합",
        "총합계",
        "월계",
        "일계",
        "total",
        "subtotal",
        "grandtotal",
        "sum",
    }
)
_TOTAL_PREFIX = re.compile(r"^(합\s*계|총\s*계|소\s*계|누\s*계|월\s*계)")


@dataclass(frozen=True)
class TableView:
    table: Table
    header_rows: tuple[int, ...]  # source row indices used as header (1 or 2 rows)
    headers: tuple[str, ...]  # composed header text per column
    data: tuple[Row, ...]
    totals: tuple[Row, ...] = ()
    skipped: tuple[tuple[int, str], ...] = ()  # (row index, reason)
    preamble: str = ""
    header_score: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def samples(self, n: int = 20) -> list[list[str]]:
        return [r.texts() for r in self.data[:n]]


def _is_texty(s: str) -> bool:
    return bool(s.strip()) and not _NUMERIC.match(s)


def _row_score(headers: Sequence[str], vocab: frozenset[str]) -> int:
    score = 0
    for h in headers:
        n = normalize_header(h)
        if not n or not _is_texty(h):
            continue
        if n in vocab:
            score += 3
        elif len(n) >= 2 and any(len(v) >= 2 and v in n for v in vocab):
            score += 1
    return score


def _has_hmerge(row: Row) -> bool:
    for c in row.cells:
        m = c.locator.get("merged")
        if isinstance(m, str) and ":" in m:
            a, b = m.split(":", 1)
            if re.sub(r"\d", "", a) != re.sub(r"\d", "", b):  # spans several columns
                return True
    return False


def _compose(parent: Row, child: Row, width: int) -> list[str]:
    out = []
    for i in range(width):
        p = parent.cell(i)
        c = child.cell(i)
        pt = p.text.strip() if p else ""
        ct = c.text.strip() if c else ""
        if pt and ct and pt != ct:
            out.append(f"{pt} {ct}")
        else:
            out.append(ct or pt)
    return out


def is_total_row(row: Row) -> bool:
    """A 합계/총계/소계 row: a total word in one of the first texty cells."""
    texty = [c.text.strip() for c in row.cells if _is_texty(c.text)]
    for t in texty[:3]:
        n = normalize_header(t)
        if n in TOTAL_WORDS or (_TOTAL_PREFIX.match(t) and len(n) <= 6):
            return True
    return False


def detect_header(table: Table, vocab: Iterable[str]) -> TableView:
    v = frozenset(normalize_header(x) for x in vocab if normalize_header(x))
    rows = [r for r in table.rows if not r.is_empty][:MAX_SCAN]
    width = max((len(r.cells) for r in table.rows), default=0)
    best: tuple[int, int, tuple[int, ...], list[str]] | None = None
    for i, r in enumerate(rows):
        single = r.texts() + [""] * (width - len(r.cells))
        s = _row_score(single, v)
        cand = (s, 0, (r.index,), single)
        if best is None or cand[0] > best[0]:
            best = cand
        if i + 1 < len(rows) and _has_hmerge(r) and rows[i + 1].index == r.index + 1:
            comp = _compose(r, rows[i + 1], width)
            s2 = _row_score(comp, v)
            child_s = _row_score(rows[i + 1].texts(), v)
            if s2 >= max(child_s, 1) and s2 >= best[0]:
                best = (s2, 1, (r.index, rows[i + 1].index), comp)
    notes: list[str] = []
    if best is None:
        return TableView(table, (), (), ())
    if best[0] < 3:
        # no known header found: first row with >= 2 text cells is the header (low confidence)
        for r in rows:
            if sum(1 for c in r.cells if _is_texty(c.text)) >= 2:
                best = (best[0], 0, (r.index,), r.texts() + [""] * (width - len(r.cells)))
                break
        notes.append("no known header vocabulary found; column mapping must be confirmed")
    header_rows = best[2]
    headers = tuple(h.strip() for h in best[3])
    last = header_rows[-1]
    first = header_rows[0]
    pre = [
        " ".join(c.text.strip() for c in r.cells if c.text.strip())
        for r in table.rows
        if r.index < first and not r.is_empty
    ]
    norm_hdr = [normalize_header(h) for h in headers]
    data: list[Row] = []
    totals: list[Row] = []
    skipped: list[tuple[int, str]] = []
    for r in table.rows:
        if r.index <= last or r.is_empty:
            continue
        texts = [normalize_header(t) for t in r.texts()]
        nonempty = [t for t in texts if t]
        same = sum(1 for i, t in enumerate(texts) if t and i < len(norm_hdr) and t == norm_hdr[i])
        if nonempty and same >= max(2, len(nonempty) // 2):
            skipped.append((r.index, "repeated header"))
            continue
        if is_total_row(r):
            totals.append(r)
            continue
        data.append(r)
    return TableView(
        table=table,
        header_rows=header_rows,
        headers=headers,
        data=tuple(data),
        totals=tuple(totals),
        skipped=tuple(skipped),
        preamble="\n".join(pre),
        header_score=best[0],
        notes=tuple(notes),
    )
