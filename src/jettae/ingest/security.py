"""Upload safety: size/row/page caps, macro and zip-bomb rejection, spreadsheet escaping.

Formulas are never evaluated anywhere in ingest. Cached formula results in XLSX are used only
as displayed values and are flagged in the locator (``"formula": True``).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePath

from jettae.ingest.cells import CorruptFile, IngestRejected


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 20 * 1024 * 1024  # 20 MiB upload
    max_rows: int = 100_000  # per document (all tables)
    max_cols: int = 200
    max_pages: int = 300
    max_sheets: int = 50
    max_uncompressed: int = 200 * 1024 * 1024  # xlsx zip content
    max_ratio: int = 200  # zip compression ratio (bomb guard)
    max_zip_entries: int = 5000


DEFAULT_LIMITS = Limits()


def check_size(content: bytes, limits: Limits) -> None:
    if len(content) > limits.max_bytes:
        raise IngestRejected(
            f"file is {len(content)} bytes; limit is {limits.max_bytes}", code="too_large"
        )
    if not content:
        raise IngestRejected("empty file", code="empty_file")


_MACRO_PARTS = re.compile(r"(^|/)(vbaProject\.bin|vbaData\.xml)$|^xl/macrosheets/", re.I)
_MACRO_CT = re.compile(rb"macroEnabled|vbaProject|ms-excel\.sheet\.binary|ms-excel\.addin", re.I)


def check_ooxml_zip(content: bytes, limits: Limits) -> list[str]:
    """Validate an OOXML (xlsx) zip container. Returns warnings; raises on rejection."""
    warnings: list[str] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as e:
        raise CorruptFile(f"not a valid zip/xlsx container: {e}", code="bad_zip") from e
    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_zip_entries:
            raise IngestRejected("too many zip entries", code="zip_entries")
        total = 0
        for info in infos:
            name = info.filename
            if name.startswith("/") or ".." in PurePath(name).parts:
                raise IngestRejected(f"unsafe path in container: {name!r}", code="zip_path")
            if _MACRO_PARTS.search(name):
                raise IngestRejected(f"macro content is not accepted ({name})", code="macro")
            total += info.file_size
            # ratio only matters for large members (small XML parts compress very well)
            if info.file_size > 1024 * 1024 and (
                info.file_size / max(info.compress_size, 1) > limits.max_ratio
            ):
                raise IngestRejected(f"suspicious compression ratio in {name}", code="zip_bomb")
            if name.startswith("xl/externalLinks/"):
                warnings.append(f"external link part ignored: {name}")
            if name.startswith("xl/activeX/") or name.startswith("xl/embeddings/"):
                warnings.append(f"embedded object ignored: {name}")
        if total > limits.max_uncompressed:
            raise IngestRejected("uncompressed size exceeds limit", code="zip_bomb")
        try:
            ct = zf.read("[Content_Types].xml")
        except KeyError as e:
            raise CorruptFile("missing [Content_Types].xml", code="bad_ooxml") from e
        if _MACRO_CT.search(ct):
            raise IngestRejected("macro-enabled or binary workbook is not accepted", code="macro")
    return warnings


def check_ole_macros(content: bytes) -> None:
    """Legacy OLE (.xls/.doc) files: reject when a VBA project stream marker is present."""
    if b"_\x00V\x00B\x00A\x00_\x00P\x00R\x00O\x00J\x00E\x00C\x00T" in content or (
        b"V\x00B\x00A\x00" in content and b"d\x00i\x00r\x00" in content
    ):
        raise IngestRejected("legacy workbook with VBA macros is not accepted", code="macro")


# ------------------------------------------------------------------ export helpers
# Characters that make a spreadsheet treat a cell as a formula (OWASP CSV injection), incl.
# full-width variants that Excel normalises.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")
_PLAIN_NUMBER = re.compile(r"^[+-]?\d{1,3}(,\d{3})*(\.\d+)?$|^[+-]?\d+(\.\d+)?$")


def escape_formula(value: object, *, allow_numbers: bool = True) -> str:
    """Make a value safe to write into CSV/XLSX: prefix ``'`` when it would start a formula.

    Plain signed numbers (``-1,000``) are left as-is when ``allow_numbers`` is true.
    """
    s = "" if value is None else str(value)
    if not s:
        return s
    if allow_numbers and _PLAIN_NUMBER.match(s):
        return s
    if s.lstrip(" ").startswith(FORMULA_PREFIXES):
        return "'" + s
    return s


def strip_formula_prefix(value: object) -> str:
    """Remove leading formula trigger characters (lossy alternative to :func:`escape_formula`)."""
    s = "" if value is None else str(value)
    if _PLAIN_NUMBER.match(s):
        return s
    while s and (s[0] in FORMULA_PREFIXES or s[0] == " "):
        s = s[1:]
    return s


def safe_filename(name: str) -> str:
    """Basename only, no path traversal, no control characters."""
    base = PurePath(name.replace("\\", "/")).name
    base = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", base).strip(" .")
    return base or "upload"
