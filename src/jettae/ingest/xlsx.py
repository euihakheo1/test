"""XLSX parsing (openpyxl, read-only streaming): every sheet, merged cells, cached formula
values (never evaluated), leading-zero ids via number formats, dates as typed values, no
floats. Resource limits are enforced by :mod:`jettae.ingest.xlsx_guard` before openpyxl
reads any cell.
"""

from __future__ import annotations

import io
import re
import warnings as _warnings
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from jettae.domain.status import DocumentStatus
from jettae.ingest.cells import (
    Cell,
    CorruptFile,
    IngestError,
    ParsedDoc,
    RawValue,
    Row,
    SourceKind,
    Table,
    col_letter,
)
from jettae.ingest.security import DEFAULT_LIMITS, Limits, check_ooxml_zip, check_size
from jettae.ingest.xlsx_guard import SheetScan, WorkbookScan, XlsxLimits, inspect_xlsx

_ZERO_PAD = re.compile(r"^0+$")


def _to_raw(value: Any) -> RawValue:
    """openpyxl value -> typed raw value without float."""
    if value is None or isinstance(value, str | bool | int | Decimal | datetime | date):
        return value
    if isinstance(value, float):
        d = Decimal(repr(value))
        return int(d) if d == d.to_integral_value() else d
    if isinstance(value, time):
        return value.isoformat()
    return str(value)


def _display(raw: RawValue, number_format: str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return "TRUE" if raw else "FALSE"
    if isinstance(raw, datetime):
        if raw.time() == time(0, 0):
            return raw.date().isoformat()
        return raw.isoformat(sep=" ")
    if isinstance(raw, date):
        return raw.isoformat()
    if isinstance(raw, int):
        fmt = (number_format or "").strip()
        if raw >= 0 and _ZERO_PAD.match(fmt):
            return str(raw).zfill(len(fmt))  # e.g. "00012345" stored as number + 0000 format
        return str(raw)
    if isinstance(raw, Decimal):
        return format(raw.normalize(), "f")
    return str(raw)


def parse_xlsx(
    content: bytes,
    *,
    filename: str = "",
    limits: Limits = DEFAULT_LIMITS,
    xlsx_limits: XlsxLimits | None = None,
) -> ParsedDoc:
    """Parse a workbook. Every resource limit is applied by :func:`inspect_xlsx` before
    openpyxl reads a single cell; openpyxl then runs in read-only (streaming) mode over
    exactly the scanned bounds, never over the file's own ``<dimension>`` claim."""
    check_size(content, limits)
    warns = check_ooxml_zip(content, limits)
    scan = inspect_xlsx(content, xlsx_limits or XlsxLimits.from_limits(limits))
    wb = _load(content, data_only=False)
    cached: Any = None
    try:
        if scan.has_formula:
            cached = _load(content, data_only=True)
            warns.append("workbook contains formulas: cached results used, nothing evaluated")
        return _read_workbook(wb, cached, scan, filename, warns)
    except IngestError:
        raise
    except Exception as e:
        # read-only mode parses cells lazily, so damage (bad shared-string index, broken
        # XML) surfaces here rather than in load_workbook; it is still a corrupt file
        raise CorruptFile(f"cannot read workbook: {type(e).__name__}: {e}", code="bad_xlsx") from e
    finally:
        wb.close()
        if cached is not None:
            cached.close()


def _load(content: bytes, *, data_only: bool) -> Any:
    try:
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")  # openpyxl style/extension warnings
            return load_workbook(
                io.BytesIO(content),
                read_only=True,
                data_only=data_only,
                keep_vba=False,
                keep_links=False,
            )
    except (InvalidFileException, KeyError, ValueError, OSError) as e:
        raise CorruptFile(f"cannot read workbook: {e}", code="bad_xlsx") from e
    except Exception as e:  # zipfile/xml errors surface as many types
        raise CorruptFile(f"cannot read workbook: {type(e).__name__}: {e}", code="bad_xlsx") from e


def _sheet_scan(ws: Any, scan: WorkbookScan) -> SheetScan:
    part = str(getattr(ws, "_worksheet_path", "")).lstrip("/")
    sc = scan.sheets.get(part)
    if sc is None:
        # openpyxl resolved a sheet to a part the guard did not scan: refuse rather than
        # iterate an unbounded part
        raise CorruptFile(f"worksheet {ws.title!r} has no readable part", code="bad_xlsx")
    return sc


def _grid(ws: Any, sc: SheetScan) -> list[tuple[Any, ...]]:
    if sc.max_row == 0 or sc.max_col == 0:
        return []
    return [
        tuple(row)
        for row in ws.iter_rows(min_row=1, max_row=sc.max_row, min_col=1, max_col=sc.max_col)
    ]


def _read_workbook(
    wb: Any, cached: Any, scan: WorkbookScan, filename: str, warns: list[str]
) -> ParsedDoc:
    seen: set[str] = set()
    epoch_1904 = bool(getattr(wb, "epoch", None) and wb.epoch.year == 1904)
    tables: list[Table] = []
    text_parts: list[str] = []
    for ws in wb.worksheets:
        sc = _sheet_scan(ws, scan)
        if sc.part in seen:
            # the scan budget counted each part once; several sheets sharing one part would
            # multiply the work past it (Excel never writes this)
            raise CorruptFile(f"worksheet part {sc.part!r} is used twice", code="bad_xlsx")
        seen.add(sc.part)
        grid = _grid(ws, sc)
        cgrid = _grid(cached[ws.title], sc) if cached is not None else None
        merged: dict[tuple[int, int], tuple[str, tuple[int, int]]] = {}
        for r0, c0, r1, c1 in sc.merged:
            coord = f"{col_letter(c0)}{r0}:{col_letter(c1)}{r1}"
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    merged[(r, c)] = (coord, (r0, c0))
        rows: list[Row] = []
        for r in range(1, len(grid) + 1):
            cells: list[Cell] = []
            for c in range(1, sc.max_col + 1):
                src_r, src_c = r, c
                loc: dict[str, Any] = {
                    "sheet": ws.title,
                    "row": r,
                    "col": c,
                    "col_letter": col_letter(c),
                    "cell": f"{col_letter(c)}{r}",
                }
                if (r, c) in merged:
                    coord, (src_r, src_c) = merged[(r, c)]
                    loc["merged"] = coord
                cell = grid[src_r - 1][src_c - 1]
                value = cell.value
                if cell.data_type == "f":
                    loc["formula"] = True
                    value = cgrid[src_r - 1][src_c - 1].value if cgrid is not None else None
                raw = _to_raw(value)
                cells.append(Cell(_display(raw, cell.number_format), loc, raw))
            rows.append(Row(r, tuple(cells)))
        # trim trailing empty rows for compactness (indices stay source-accurate)
        while rows and rows[-1].is_empty:
            rows.pop()
        tables.append(
            Table(
                ws.title,
                tuple(rows),
                {"sheet_state": ws.sheet_state, "epoch_1904": epoch_1904},
            )
        )
        if ws.sheet_state != "visible":
            warns.append(f"sheet {ws.title!r} is {ws.sheet_state}")
        text_parts.append(f"[{ws.title}]")
        text_parts.extend("\t".join(row.texts()) for row in rows)
    return ParsedDoc(
        source_kind=SourceKind.XLSX,
        status=DocumentStatus.PARSED,
        tables=tables,
        text="\n".join(text_parts),
        filename=filename,
        warnings=warns,
        meta={"epoch_1904": epoch_1904, "sheets": [t.name for t in tables]},
    )
