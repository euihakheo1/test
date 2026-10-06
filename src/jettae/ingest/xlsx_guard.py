"""Resource limits for XLSX applied *before* openpyxl touches the workbook.

Why a separate pre-scan: openpyxl trusts the file. In normal mode it materialises every cell
of every merged range (one ``<mergeCell ref="A1:XFD1048576"/>`` is 17 billion objects); in
read-only mode it sizes iteration from the ``<dimension>`` tag, which the file can set to
``A1:XFD1048576`` while holding three cells. A few-kilobyte upload could therefore pin a
worker for hours or exhaust memory. This module bounds the work from what the file actually
contains:

1. Zip container: entry count, duplicate names, per-entry and total uncompressed size,
   compression ratio (per large entry and overall). ``zipfile`` stops decompressing at the
   declared size and checks the CRC, so declared sizes bound real work.
2. Every XML part: the prolog is checked without a full parse; a DTD (``<!DOCTYPE``, the
   only place entities can be declared) is refused. The root element identifies worksheets,
   the workbook and the shared-string table regardless of file names.
3. Every worksheet part is stream-scanned with ``lxml.iterparse`` (no entity resolution, no
   network, no huge-tree mode), counting rows, the largest row/column index, cells, merged
   ranges and merged area. Each cap stops the scan as soon as it is crossed. The
   ``<dimension>`` tag is never read.

The caller then iterates exactly the scanned bounds (``SheetScan.max_row/max_col``) and maps
openpyxl's sheets to scanned parts by path; a sheet openpyxl loads that was not scanned is
treated as a corrupt file (fail closed).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import IO, Any

from lxml import etree

from jettae.ingest.cells import CorruptFile, IngestRejected
from jettae.ingest.security import Limits

EXCEL_MAX_ROW = 1_048_576
EXCEL_MAX_COL = 16_384
_REF = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d{1,7})$")
_ROOT = re.compile(r"<([A-Za-z_][\w.\-]*(?::[A-Za-z_][\w.\-]*)?)")


@dataclass(frozen=True)
class XlsxLimits:
    """Caps for one workbook. Defaults follow :class:`~jettae.ingest.security.Limits`
    (rows, columns, sheets, zip sizes) plus XLSX-specific caps. ``max_cell_area`` bounds the
    dense grid the parser builds (sum over sheets of rows x columns), which is what makes
    a sparse file with one far-away cell expensive."""

    max_rows: int = 100_000  # sum of the largest row index of each sheet
    max_cols: int = 200
    max_sheets: int = 50
    max_cells: int = 1_000_000  # <c> elements in all sheets
    max_cell_area: int = 1_000_000  # sum of max_row * max_col over sheets
    max_merged_ranges: int = 10_000
    max_merged_area: int = 1_000_000  # cells covered by merged ranges, all sheets
    max_row_elements: int = 200_000  # <row> elements in all sheets
    max_shared_strings: int = 1_000_000
    max_zip_entries: int = 5000
    max_uncompressed: int = 200 * 1024 * 1024
    max_entry_uncompressed: int = 128 * 1024 * 1024
    max_ratio: int = 200
    max_prolog_bytes: int = 64 * 1024

    @classmethod
    def from_limits(cls, limits: Limits, **overrides: Any) -> XlsxLimits:
        base: dict[str, Any] = {
            "max_rows": limits.max_rows,
            "max_cols": limits.max_cols,
            "max_sheets": limits.max_sheets,
            "max_zip_entries": limits.max_zip_entries,
            "max_uncompressed": limits.max_uncompressed,
            "max_ratio": limits.max_ratio,
            "max_row_elements": max(2 * limits.max_rows, 1),
        }
        base.update(overrides)
        return cls(**base)


@dataclass
class SheetScan:
    part: str
    max_row: int = 0
    max_col: int = 0
    cells: int = 0
    rows: int = 0
    merged: list[tuple[int, int, int, int]] = field(default_factory=list)  # r0, c0, r1, c1
    has_formula: bool = False

    @property
    def area(self) -> int:
        return self.max_row * self.max_col


@dataclass
class WorkbookScan:
    sheets: dict[str, SheetScan]  # zip part name -> scan
    has_formula: bool
    shared_strings: int
    sheet_entries: int  # <sheet> elements in the workbook part


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def parse_ref(ref: str) -> tuple[int, int]:
    """``"B12"`` -> (12, 2). Out-of-grid or malformed references make the file corrupt."""
    m = _REF.match(ref.strip())
    if not m:
        raise CorruptFile(f"invalid cell reference {ref[:32]!r}", code="bad_xlsx")
    row, col = int(m.group(2)), col_index(m.group(1))
    if not (1 <= row <= EXCEL_MAX_ROW and 1 <= col <= EXCEL_MAX_COL):
        raise CorruptFile(f"cell reference {ref[:32]!r} is outside the sheet", code="bad_xlsx")
    return row, col


def _parse_row_number(raw: str) -> int:
    # openpyxl accepts integral floats ("5.0") for row numbers; mirror it
    try:
        n = int(raw)
    except ValueError:
        try:
            f = float(raw)
        except ValueError:
            raise CorruptFile(f"invalid row number {raw[:16]!r}", code="bad_xlsx") from None
        if not f.is_integer():
            raise CorruptFile(f"invalid row number {raw[:16]!r}", code="bad_xlsx") from None
        n = int(f)
    if not 1 <= n <= EXCEL_MAX_ROW:
        raise CorruptFile(f"row number {n} is outside the sheet", code="bad_xlsx")
    return n


# ------------------------------------------------------------------------------- zip level
def check_container(zf: zipfile.ZipFile, lim: XlsxLimits) -> None:
    infos = zf.infolist()
    if len(infos) > lim.max_zip_entries:
        raise IngestRejected(f"{len(infos)} zip entries exceed limit", code="zip_entries")
    names: set[str] = set()
    total = total_compressed = 0
    for info in infos:
        if info.filename in names:
            # two members with one name: readers may disagree on which one is "the" part
            raise CorruptFile(f"duplicate zip member {info.filename!r}", code="bad_zip")
        names.add(info.filename)
        if info.file_size > lim.max_entry_uncompressed:
            raise IngestRejected(
                f"zip member {info.filename!r} expands to {info.file_size} bytes",
                code="zip_entry_size",
            )
        if info.file_size > 1024 * 1024 and (
            info.file_size / max(info.compress_size, 1) > lim.max_ratio
        ):
            raise IngestRejected(
                f"suspicious compression ratio in {info.filename}", code="zip_bomb"
            )
        total += info.file_size
        total_compressed += info.compress_size
    if total > lim.max_uncompressed:
        raise IngestRejected("uncompressed size exceeds limit", code="zip_bomb")
    if total > 1024 * 1024 and total / max(total_compressed, 1) > lim.max_ratio:
        raise IngestRejected("suspicious overall compression ratio", code="zip_bomb")


def xml_root(zf: zipfile.ZipFile, name: str, lim: XlsxLimits) -> str | None:
    """Local name of the root element of ``name``, or None when the member is not XML.
    Raises when the prolog holds a DTD or is longer than ``max_prolog_bytes``."""
    with zf.open(name) as fh:
        head = fh.read(lim.max_prolog_bytes)
    truncated = len(head) == lim.max_prolog_bytes
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = head.decode("utf-16", errors="ignore")
    else:
        text = head.decode("utf-8", errors="ignore")
    text = text.lstrip("﻿")
    pos, n = 0, len(text)
    while True:
        while pos < n and text[pos] in " \t\r\n":
            pos += 1
        if pos >= n:
            if truncated:
                raise IngestRejected(f"XML prolog of {name!r} too long", code="xml_prolog")
            return None
        if text.startswith("<?", pos):
            end = text.find("?>", pos)
        elif text.startswith("<!--", pos):
            end = text.find("-->", pos)
        elif text.startswith("<!", pos):
            raise IngestRejected(
                f"XML part {name!r} declares a DTD; entity declarations are not accepted",
                code="xml_dtd",
            )
        elif text.startswith("<", pos):
            m = _ROOT.match(text, pos)
            if not m:
                return None
            return m.group(1).rsplit(":", 1)[-1]
        else:
            return None  # binary member (image, printer settings, ...)
        if end < 0:
            if truncated:
                raise IngestRejected(f"XML prolog of {name!r} too long", code="xml_prolog")
            return None
        pos = end + 2


def _iterparse(fh: IO[bytes], events: tuple[str, ...], tags: tuple[str, ...]) -> Any:
    return etree.iterparse(
        fh,
        events=events,
        tag=tags,
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        recover=False,
        remove_comments=True,
    )


def _drop_done(el: Any) -> None:
    """Free a processed element and its already processed siblings (bounded memory)."""
    el.clear(keep_tail=False)
    parent = el.getparent()
    if parent is not None:
        while el.getprevious() is not None:
            del parent[0]


class _Budget:
    """Workbook-wide counters shared by the sheet scans; every increment checks its cap."""

    def __init__(self, lim: XlsxLimits) -> None:
        self.lim = lim
        self.rows_before = 0  # sum of max_row of sheets already scanned
        self.area_before = 0
        self.cells = 0
        self.row_elements = 0
        self.merged_ranges = 0
        self.merged_area = 0

    def extent(self, sc: SheetScan, row: int, col: int) -> None:
        lim = self.lim
        if row > sc.max_row:
            sc.max_row = row
            if self.rows_before + row > lim.max_rows:
                raise IngestRejected(
                    f"rows exceed limit {lim.max_rows} (row {row} in {sc.part})", code="rows"
                )
        if col > sc.max_col:
            sc.max_col = col
            if col > lim.max_cols:
                raise IngestRejected(
                    f"{sc.part}: column {col} exceeds limit {lim.max_cols}", code="cols"
                )
        if self.area_before + sc.max_row * sc.max_col > lim.max_cell_area:
            raise IngestRejected(
                f"sheet grid {sc.max_row}x{sc.max_col} exceeds {lim.max_cell_area} cells",
                code="cell_area",
            )


def scan_sheet(fh: IO[bytes], part: str, budget: _Budget) -> SheetScan:
    lim = budget.lim
    sc = SheetScan(part)
    row_counter = col_counter = 0
    try:
        for event, el in _iterparse(
            fh, ("start", "end"), ("{*}row", "{*}c", "{*}mergeCell", "{*}f")
        ):
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "c":
                if event == "start":
                    ref = el.get("r")
                    if ref:
                        row, col = parse_ref(ref)
                    else:
                        row, col = row_counter, col_counter + 1
                        if row < 1 or col > EXCEL_MAX_COL:
                            raise CorruptFile(f"cell outside the sheet in {part}", code="bad_xlsx")
                    col_counter = col
                    sc.cells += 1
                    budget.cells += 1
                    if budget.cells > lim.max_cells:
                        raise IngestRejected(f"cells exceed limit {lim.max_cells}", code="cells")
                    budget.extent(sc, row, col)
                else:
                    _drop_done(el)
            elif tag == "row":
                if event == "start":
                    raw = el.get("r")
                    row_counter = _parse_row_number(raw) if raw else row_counter + 1
                    if row_counter > EXCEL_MAX_ROW:
                        raise CorruptFile(f"row outside the sheet in {part}", code="bad_xlsx")
                    col_counter = 0
                    sc.rows += 1
                    budget.row_elements += 1
                    if budget.row_elements > lim.max_row_elements:
                        raise IngestRejected(
                            f"row elements exceed limit {lim.max_row_elements}", code="rows"
                        )
                else:
                    _drop_done(el)
            elif tag == "mergeCell" and event == "end":
                ref = el.get("ref") or ""
                a, _, b = ref.partition(":")
                r0, c0 = parse_ref(a)
                r1, c1 = parse_ref(b) if b else (r0, c0)
                r0, r1 = sorted((r0, r1))
                c0, c1 = sorted((c0, c1))
                budget.merged_ranges += 1
                if budget.merged_ranges > lim.max_merged_ranges:
                    raise IngestRejected(
                        f"merged ranges exceed limit {lim.max_merged_ranges}",
                        code="merged_ranges",
                    )
                budget.merged_area += (r1 - r0 + 1) * (c1 - c0 + 1)
                if budget.merged_area > lim.max_merged_area:
                    raise IngestRejected(
                        f"merged area exceeds limit {lim.max_merged_area} cells",
                        code="merged_area",
                    )
                # openpyxl (normal mode) counts merged ranges in a sheet's size; so do we
                budget.extent(sc, r1, c1)
                sc.merged.append((r0, c0, r1, c1))
                _drop_done(el)
            elif tag == "f" and event == "end":
                sc.has_formula = True
    except etree.XMLSyntaxError as e:
        raise CorruptFile(f"malformed worksheet XML in {part}: {e}", code="bad_xlsx") from e
    budget.rows_before += sc.max_row
    budget.area_before += sc.area
    return sc


def _count(fh: IO[bytes], part: str, tag: str, cap: int, code: str) -> int:
    n = 0
    try:
        for _, el in _iterparse(fh, ("end",), (f"{{*}}{tag}",)):
            n += 1
            if n > cap:
                raise IngestRejected(f"{tag} elements in {part} exceed limit {cap}", code=code)
            _drop_done(el)
    except etree.XMLSyntaxError as e:
        raise CorruptFile(f"malformed XML in {part}: {e}", code="bad_xlsx") from e
    return n


def inspect_xlsx(content: bytes, lim: XlsxLimits) -> WorkbookScan:
    """Check the container and stream-scan every worksheet. Raises
    :class:`IngestRejected` (limit) or :class:`CorruptFile` (malformed)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as e:
        raise CorruptFile(f"not a valid zip/xlsx container: {e}", code="bad_zip") from e
    try:
        with zf:
            check_container(zf, lim)
            roots: dict[str, str] = {}
            for info in zf.infolist():
                if info.is_dir():
                    continue
                root = xml_root(zf, info.filename, lim)
                if root is not None:
                    roots[info.filename] = root
            worksheets = [n for n, r in roots.items() if r in ("worksheet", "dialogsheet")]
            if len(worksheets) > lim.max_sheets:
                raise IngestRejected(f"{len(worksheets)} sheets exceed limit", code="sheets")
            sheet_entries = 0
            shared = 0
            for name, root in roots.items():
                if root == "workbook":
                    with zf.open(name) as fh:
                        sheet_entries += _count(fh, name, "sheet", lim.max_sheets, "sheets")
                elif root == "sst":
                    with zf.open(name) as fh:
                        shared += _count(fh, name, "si", lim.max_shared_strings, "shared_strings")
            budget = _Budget(lim)
            scans: dict[str, SheetScan] = {}
            for name in worksheets:
                with zf.open(name) as fh:
                    scans[name] = scan_sheet(fh, name, budget)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError) as e:
        # CRC mismatch, truncated member, encrypted or unsupported compression
        raise CorruptFile(f"damaged zip member: {e}", code="bad_zip") from e
    return WorkbookScan(
        sheets=scans,
        has_formula=any(s.has_formula for s in scans.values()),
        shared_strings=shared,
        sheet_entries=sheet_entries,
    )
