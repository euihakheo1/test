"""Typed value parsing for cells: KRW amounts, business dates, identifiers.

Rules (SPEC §5): integer won only (a fractional won amount is an error, never rounded
silently); explicit negative markers (``-``, ``(1,000)``, ``△``/``▲``, trailing ``-``);
Excel serial dates only when the column is a date column; identifiers stay text so leading
zeros survive. No ``float`` leaves this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from jettae.ingest.cells import Cell

_SPACES = re.compile(r"[\s 　]+")
_CURRENCY = re.compile(r"(원|₩|\\|KRW|krw)")
_NEG_MARK = ("△", "▲", "-", "−", "－")
_NUM = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$|^\d+(\.\d+)?$")


class ValueParseError(ValueError):
    """A cell could not be converted to the requested type (kept as a row issue)."""


@dataclass(frozen=True)
class DateValue:
    value: date
    time: time | None = None
    note: str | None = None  # e.g. "excel_serial_1900"


def parse_amount(cell: Cell | str, *, allow_empty: bool = True) -> int | None:
    """Parse a KRW amount in whole won. Returns ``None`` for an empty cell."""
    raw = cell.raw if isinstance(cell, Cell) else None
    text = cell.text if isinstance(cell, Cell) else cell
    if isinstance(raw, bool):
        raise ValueParseError(f"boolean is not an amount: {text!r}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, Decimal):
        if raw != raw.to_integral_value():
            raise ValueParseError(f"fractional won amount: {raw}")
        return int(raw)
    s = _SPACES.sub("", text or "")
    if not s or s in ("-", "−"):
        if allow_empty:
            return None
        raise ValueParseError("empty amount")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = _CURRENCY.sub("", s)
    if s.startswith("+"):
        s = s[1:]
    if s and s[0] in _NEG_MARK:
        neg, s = (not neg), s[1:]
    if s.endswith("-") and len(s) > 1:  # "1,000-" (trailing minus, some bank exports)
        neg, s = (not neg), s[:-1]
    s = _CURRENCY.sub("", s)
    if not _NUM.match(s):
        raise ValueParseError(f"not an amount: {text!r}")
    try:
        d = Decimal(s.replace(",", ""))
    except InvalidOperation as e:  # pragma: no cover - regex guards this
        raise ValueParseError(f"not an amount: {text!r}") from e
    if d != d.to_integral_value():
        raise ValueParseError(f"fractional won amount: {text!r}")
    n = int(d)
    return -n if neg else n


_DATE_PATTERNS = [
    # 2025-08-07 / 2025.08.07 / 2025/08/07 / 2025. 8. 7. with optional time
    re.compile(
        r"^(?P<y>\d{4})\s*[-./]\s*(?P<m>\d{1,2})\s*[-./]\s*(?P<d>\d{1,2})\.?"
        r"(?:[ T]+(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?)?$"
    ),
    # 20250807 / 20250807 134500 / 20250807134500
    re.compile(
        r"^(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?:\s*(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})?)?$"
    ),
    # 2025년 8월 7일
    re.compile(r"^(?P<y>\d{4})년\s*(?P<m>\d{1,2})월\s*(?P<d>\d{1,2})일?$"),
]
EXCEL_1900 = date(1899, 12, 30)  # serial 1 = 1900-01-01 (Lotus leap-year bug accounted)
EXCEL_1904 = date(1904, 1, 1)
_SERIAL_MIN, _SERIAL_MAX = 20000, 80000  # ~1954 .. ~2119: plausible business dates only


def excel_serial_to_date(serial: int, *, epoch_1904: bool = False) -> date:
    if not (_SERIAL_MIN <= serial <= _SERIAL_MAX):
        raise ValueParseError(f"implausible Excel serial date: {serial}")
    return (EXCEL_1904 if epoch_1904 else EXCEL_1900) + timedelta(days=serial)


def parse_date(
    cell: Cell | str, *, allow_serial: bool = True, epoch_1904: bool = False
) -> DateValue | None:
    """Parse a business date (and optional time). ``None`` for an empty cell."""
    raw = cell.raw if isinstance(cell, Cell) else None
    text = (cell.text if isinstance(cell, Cell) else cell) or ""
    if isinstance(raw, datetime):
        rt = raw.time()
        return DateValue(raw.date(), rt if rt != time(0, 0) else None)
    if isinstance(raw, date):
        return DateValue(raw)
    if isinstance(raw, int | Decimal) and not isinstance(raw, bool):
        if not allow_serial:
            raise ValueParseError(f"number in date column: {text!r}")
        if isinstance(raw, Decimal) and raw != raw.to_integral_value():
            # date + time fraction; keep the date part only, record it
            n = int(raw)
            return DateValue(excel_serial_to_date(n, epoch_1904=epoch_1904), None, "excel_serial")
        return DateValue(
            excel_serial_to_date(int(raw), epoch_1904=epoch_1904), None, "excel_serial"
        )
    s = text.strip()
    if not s:
        return None
    for pat in _DATE_PATTERNS:
        m = pat.match(s)
        if m:
            gd = m.groupdict()
            try:
                d = date(int(gd["y"]), int(gd["m"]), int(gd["d"]))
                t: time | None = None
                if gd.get("H"):
                    t = time(int(gd["H"]), int(gd["M"]), int(gd.get("S") or 0))
            except ValueError as e:
                raise ValueParseError(f"invalid date: {text!r}") from e
            return DateValue(d, t)
    if allow_serial and re.fullmatch(r"\d{5}", s):
        return DateValue(excel_serial_to_date(int(s), epoch_1904=epoch_1904), None, "excel_serial")
    raise ValueParseError(f"not a date: {text!r}")


def parse_time(cell: Cell | str) -> time | None:
    raw = cell.raw if isinstance(cell, Cell) else None
    text = (cell.text if isinstance(cell, Cell) else cell).strip()
    if isinstance(raw, time):  # pragma: no cover - openpyxl gives time objects
        return raw
    if isinstance(raw, datetime):
        return raw.time()
    if not text:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text) or re.fullmatch(
        r"(\d{2})(\d{2})(\d{2})?", text
    )
    if not m:
        raise ValueParseError(f"not a time: {text!r}")
    try:
        return time(int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    except ValueError as e:
        raise ValueParseError(f"invalid time: {text!r}") from e


def parse_text(cell: Cell | str) -> str | None:
    text = (cell.text if isinstance(cell, Cell) else cell).strip()
    return text or None


def parse_id(cell: Cell | str) -> str | None:
    """Identifier as text (leading zeros preserved, inner whitespace removed)."""
    text = cell.text if isinstance(cell, Cell) else cell
    s = _SPACES.sub("", text or "")
    return s or None


def parse_brn(cell: Cell | str) -> str | None:
    """사업자등록번호 → ``123-45-67890`` when it has 10 digits, else the digits as given."""
    s = parse_id(cell)
    if s is None:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    return s


def parse_int(cell: Cell | str) -> int | None:
    raw = cell.raw if isinstance(cell, Cell) else None
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    text = (cell.text if isinstance(cell, Cell) else cell).strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d+)\s*(일|days?)?", text)
    if not m:
        raise ValueParseError(f"not an integer: {text!r}")
    return int(m.group(1))
