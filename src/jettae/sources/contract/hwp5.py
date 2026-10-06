"""Text extraction from HWP 5.0 binary documents (한글 .hwp).

Format (public HWP 5.0 spec): an OLE2 compound file with ``FileHeader`` and
``BodyText/Section{n}`` streams; sections are raw-deflate compressed when header bit 0 is
set. Each section is a sequence of records (tag/level/size header); paragraph text is in
``HWPTAG_PARA_TEXT`` (tag 67) as UTF-16LE with inline/extended control characters that
occupy 8 code units. Table cells are ordinary paragraphs nested in the record stream, so a
sequential scan yields all text in reading order.

Distribution (배포용) and password-protected documents are reported, not decoded.
"""

from __future__ import annotations

import re
import struct
import zlib
from array import array
from dataclasses import dataclass

from jettae.sources.contract.cfb import CfbError, CompoundFile

HWPTAG_BEGIN = 0x010
HWPTAG_PARA_HEADER = HWPTAG_BEGIN + 50
HWPTAG_PARA_TEXT = HWPTAG_BEGIN + 51
SIGNATURE = b"HWP Document File"

_CHAR_CTRL = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}  # 1 code unit
_MAP = {9: "\t", 10: "\n", 24: "-", 30: " ", 31: " "}


class HwpError(ValueError):
    pass


@dataclass(frozen=True)
class Paragraph:
    section: int
    index: int  # paragraph index within the section (PARA_HEADER order)
    level: int  # record nesting level (>0: inside a table cell / text box)
    text: str


def _decode_para_text(payload: bytes) -> str:
    n = len(payload) // 2
    units = array("H")
    units.frombytes(payload[: n * 2])
    if struct.pack("=H", 1) != struct.pack("<H", 1):  # pragma: no cover - big-endian host
        units.byteswap()
    out: list[str] = []
    kept = array("H")
    i = 0
    while i < n:
        c = units[i]
        if c >= 32:
            kept.append(c)
            i += 1
            continue
        if kept:
            out.append(kept.tobytes().decode("utf-16-le", errors="replace"))
            kept = array("H")
        if c in _MAP:
            out.append(_MAP[c])
        i += 1 if c in _CHAR_CTRL else 8
    if kept:
        out.append(kept.tobytes().decode("utf-16-le", errors="replace"))
    return "".join(out)


def iter_records(data: bytes) -> list[tuple[int, int, bytes]]:
    recs = []
    pos, n = 0, len(data)
    while pos + 4 <= n:
        (h,) = struct.unpack_from("<I", data, pos)
        pos += 4
        tag, level, size = h & 0x3FF, (h >> 10) & 0x3FF, (h >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > n:
                break
            (size,) = struct.unpack_from("<I", data, pos)
            pos += 4
        if pos + size > n:
            raise HwpError("truncated record")
        recs.append((tag, level, data[pos : pos + size]))
        pos += size
    return recs


def read_hwp(data: bytes) -> list[Paragraph]:
    try:
        cf = CompoundFile(data)
    except CfbError as e:
        raise HwpError(f"not a readable HWP 5.0 file: {e}") from e
    if not cf.exists("FileHeader"):
        raise HwpError("FileHeader stream missing (not HWP 5.0)")
    fh = cf.read("FileHeader")
    if not fh.startswith(SIGNATURE):
        raise HwpError("bad HWP signature")
    (props,) = struct.unpack_from("<I", fh, 36)
    compressed, password, distribution = bool(props & 1), bool(props & 2), bool(props & 4)
    if password:
        raise HwpError("password-protected HWP document")
    if distribution:
        raise HwpError("distribution (배포용) HWP document: body text is encrypted")
    sec_names = sorted(
        (p for p in cf.list_streams() if re.fullmatch(r"BodyText/Section\d+", p)),
        key=lambda p: int(p.rsplit("Section", 1)[1]),
    )
    if not sec_names:
        raise HwpError("no BodyText sections")
    paras: list[Paragraph] = []
    for name in sec_names:
        sec = int(name.rsplit("Section", 1)[1])
        raw = cf.read(name)
        if compressed:
            try:
                raw = zlib.decompress(raw, -15)
            except zlib.error as e:
                raise HwpError(f"{name}: cannot inflate ({e})") from e
        idx = -1
        level = 0
        for tag, lvl, payload in iter_records(raw):
            if tag == HWPTAG_PARA_HEADER:
                idx += 1
                level = lvl
            elif tag == HWPTAG_PARA_TEXT:
                text = _decode_para_text(payload)
                if text.strip():
                    paras.append(Paragraph(sec, max(idx, 0), level, text))
    return paras
