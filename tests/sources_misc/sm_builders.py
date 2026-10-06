"""Hand-written builders for unit-test fixtures (NOT real data): a minimal OLE2 compound
file writer, HWP 5.0 record encoding and a tiny HWPX zip."""

from __future__ import annotations

import hashlib
import io
import struct
import zipfile
import zlib

SECTOR = 512
MINI = 64
CUTOFF = 4096
END = 0xFFFFFFFE
FREE = 0xFFFFFFFF
FATSECT = 0xFFFFFFFD


def _pad(b: bytes, n: int) -> bytes:
    return b + b"\0" * ((-len(b)) % n)


def build_cfb(streams: dict[str, bytes]) -> bytes:
    """Compound file with at most one storage level ("Storage/Stream" paths)."""
    # directory entries: (name, type, children names) — root=0
    entries: list[dict] = [{"name": "Root Entry", "type": 5, "children": []}]
    index: dict[str, int] = {}
    for path in sorted(streams):
        parts = path.split("/")
        parent = 0
        if len(parts) == 2:
            if parts[0] not in index:
                entries.append({"name": parts[0], "type": 1, "children": []})
                index[parts[0]] = len(entries) - 1
                entries[0]["children"].append(index[parts[0]])
            parent = index[parts[0]]
        entries.append({"name": parts[-1], "type": 2, "children": [], "data": streams[path]})
        entries[parent]["children"].append(len(entries) - 1)

    # mini stream for small streams
    mini = b""
    minifat: list[int] = []
    big: list[tuple[int, bytes]] = []
    for i, e in enumerate(entries):
        if e["type"] != 2:
            continue
        d = e["data"]
        e["size"] = len(d)
        if len(d) < CUTOFF:
            start = len(mini) // MINI
            n = max(1, -(-len(d) // MINI)) if d else 0
            e["start"] = start if d else END
            for k in range(n):
                minifat.append(start + k + 1 if k < n - 1 else END)
            mini += _pad(d, MINI) if d else b""
        else:
            big.append((i, d))

    dir_bytes_len = _pad(b"\0" * (128 * len(entries)), SECTOR)
    n_dir = len(dir_bytes_len) // SECTOR
    minifat_b = _pad(struct.pack(f"<{len(minifat)}I", *minifat), SECTOR) if minifat else b""
    n_mf = len(minifat_b) // SECTOR
    mini_b = _pad(mini, SECTOR)
    n_ms = len(mini_b) // SECTOR
    big_secs = [len(_pad(d, SECTOR)) // SECTOR for _, d in big]
    n_fat = 1
    total = n_fat + n_dir + n_mf + n_ms + sum(big_secs)
    assert total <= SECTOR // 4, "builder supports a single FAT sector"

    fat = [FREE] * (SECTOR // 4)
    pos = 0
    fat[pos] = FATSECT
    pos += 1

    def chain(n: int) -> int:
        nonlocal pos
        if n == 0:
            return END
        start = pos
        for k in range(n):
            fat[pos + k] = pos + k + 1 if k < n - 1 else END
        pos += n
        return start

    dir_start = chain(n_dir)
    mf_start = chain(n_mf)
    ms_start = chain(n_ms)
    entries[0]["start"] = ms_start if mini else END
    entries[0]["size"] = len(mini)
    for (i, _d), n in zip(big, big_secs, strict=True):
        entries[i]["start"] = chain(n)

    # simple binary trees: children chained through right siblings
    for e in entries:
        e.setdefault("left", FREE)
        e.setdefault("right", FREE)
        e.setdefault("child", FREE)
    for e in entries:
        kids = e["children"]
        if kids:
            e["child"] = kids[0]
            for a, b in zip(kids, kids[1:], strict=False):
                entries[a]["right"] = b

    dir_b = b""
    for e in entries:
        name = e["name"].encode("utf-16-le") + b"\0\0"
        rec = name.ljust(64, b"\0")
        rec += struct.pack("<HBB", len(name), e["type"], 1)
        rec += struct.pack("<III", e["left"], e["right"], e["child"])
        rec += b"\0" * 16 + b"\0" * 4 + b"\0" * 16
        rec += struct.pack("<III", e.get("start", END), e.get("size", 0), 0)
        assert len(rec) == 128
        dir_b += rec
    dir_b = _pad(dir_b, SECTOR)

    header = bytearray(SECTOR)
    header[0:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHH", header, 0x18, 0x3E, 3, 0xFFFE, 9)
    struct.pack_into("<H", header, 0x20, 6)
    struct.pack_into("<II", header, 0x2C, n_fat, dir_start)
    struct.pack_into("<IIIII", header, 0x38, CUTOFF, mf_start, n_mf, END, 0)
    difat = [0] + [FREE] * 108
    struct.pack_into("<109I", header, 0x4C, *difat)

    body = struct.pack(f"<{len(fat)}I", *fat) + dir_b + minifat_b + mini_b
    for _, d in big:
        body += _pad(d, SECTOR)
    return bytes(header) + body


def record(tag: int, level: int, payload: bytes) -> bytes:
    size = len(payload)
    if size >= 0xFFF:
        return struct.pack("<II", tag | (level << 10) | (0xFFF << 20), size) + payload
    return struct.pack("<I", tag | (level << 10) | (size << 20)) + payload


def para_text(*parts: str | int) -> bytes:
    """UTF-16LE para text; ints are control codes (extended/inline ones get 7 filler units)."""
    out = b""
    for p in parts:
        if isinstance(p, int):
            if p in (0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31):
                out += struct.pack("<H", p)
            else:
                out += struct.pack("<H", p) + b"\x01\x00" * 6 + struct.pack("<H", p)
        else:
            out += p.encode("utf-16-le")
    return out


def build_hwp(
    paragraphs: list[tuple[int, list[str | int]]], props: int = 1, big: bool = False
) -> bytes:
    """paragraphs: (level, parts). props bit0 compressed, bit1 password, bit2 distribution."""
    recs = b""
    for level, parts in paragraphs:
        recs += record(66, level, b"\0" * 22)
        recs += record(67, level + 1, para_text(*parts, 13))
    if big:  # force the section into regular sectors (>= 4096 bytes after compression)
        noise = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(200))
        recs += record(66, 0, b"\0" * 22) + record(67, 1, noise)  # 6400 incompressible bytes
    section = recs
    if props & 1:
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        section = c.compress(recs) + c.flush()
    header = b"HWP Document File".ljust(32, b"\0") + struct.pack("<II", 0x05000300, props)
    header = header.ljust(256, b"\0")
    return build_cfb({"FileHeader": header, "BodyText/Section0": section, "PrvText": b"x\0"})


def build_hwpx(paragraph_xml: str) -> bytes:
    ns = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'
    head = '<?xml version="1.0" encoding="UTF-8"?>'
    xml = f'{head}<hs:sec xmlns:hs="urn:s" {ns}>{paragraph_xml}</hs:sec>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("Contents/section0.xml", xml)
    return buf.getvalue()
