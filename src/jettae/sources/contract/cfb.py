"""Minimal read-only reader for OLE2 / Compound File Binary (MS-CFB), as used by HWP 5.0.

Only what is needed to read named streams: header, DIFAT/FAT, directory tree, mini stream.
Chains are bounded (loop / out-of-range protection) so a corrupt file raises
:class:`CfbError` instead of hanging.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
NOSTREAM = 0xFFFFFFFF


class CfbError(ValueError):
    pass


@dataclass(frozen=True)
class DirEntry:
    sid: int
    name: str
    type: int  # 1 storage, 2 stream, 5 root
    left: int
    right: int
    child: int
    start: int
    size: int


class CompoundFile:
    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or data[:8] != MAGIC:
            raise CfbError("not an OLE2 compound file")
        self.data = data
        (major,) = struct.unpack_from("<H", data, 0x1A)
        (self.sector_shift,) = struct.unpack_from("<H", data, 0x1E)
        (self.mini_shift,) = struct.unpack_from("<H", data, 0x20)
        if self.sector_shift not in (9, 12) or self.mini_shift != 6:
            raise CfbError("unsupported sector size")
        self.ssize = 1 << self.sector_shift
        self.msize = 1 << self.mini_shift
        n_fat, first_dir = struct.unpack_from("<II", data, 0x2C)
        self.cutoff, first_minifat, n_minifat, first_difat, n_difat = struct.unpack_from(
            "<IIIII", data, 0x38
        )
        self.major = major
        self.n_sectors = (len(data) - self.ssize) // self.ssize + 1
        fat_sectors = list(struct.unpack_from("<109I", data, 0x4C))
        sec, seen = first_difat, 0
        per = self.ssize // 4 - 1
        while sec not in (ENDOFCHAIN, FREESECT) and seen < n_difat:
            buf = self._sector(sec)
            vals = struct.unpack_from(f"<{per + 1}I", buf, 0)
            fat_sectors.extend(vals[:per])
            sec = vals[per]
            seen += 1
        fat_sectors = [s for s in fat_sectors if s not in (FREESECT, ENDOFCHAIN)][:n_fat]
        fat_bytes = b"".join(self._sector(s) for s in fat_sectors)
        self.fat = list(struct.unpack(f"<{len(fat_bytes) // 4}I", fat_bytes))
        dir_bytes = self._chain_bytes(first_dir, self.fat, self._sector)
        self.entries = [
            self._entry(i, dir_bytes[i * 128 : (i + 1) * 128]) for i in range(len(dir_bytes) // 128)
        ]
        if not self.entries or self.entries[0].type != 5:
            raise CfbError("missing root entry")
        if n_minifat and first_minifat not in (ENDOFCHAIN, FREESECT):
            mf = self._chain_bytes(first_minifat, self.fat, self._sector)
            self.minifat = list(struct.unpack(f"<{len(mf) // 4}I", mf))
        else:
            self.minifat = []
        root = self.entries[0]
        self.ministream = (
            self._chain_bytes(root.start, self.fat, self._sector)[: root.size]
            if root.start not in (ENDOFCHAIN, FREESECT)
            else b""
        )
        self.paths = self._walk()

    # -- low level -------------------------------------------------------------
    def _sector(self, sec: int) -> bytes:
        if sec >= self.n_sectors:
            raise CfbError(f"sector {sec} out of range")
        off = (sec + 1) * self.ssize
        return self.data[off : off + self.ssize]

    def _mini(self, sec: int) -> bytes:
        off = sec * self.msize
        if off >= len(self.ministream):
            raise CfbError(f"mini sector {sec} out of range")
        return self.ministream[off : off + self.msize]

    @staticmethod
    def _chain_bytes(start: int, table: list[int], read: object) -> bytes:
        out = []
        sec, seen = start, set()
        reader = read  # callable
        while sec not in (ENDOFCHAIN, FREESECT):
            if sec in seen or sec >= len(table):
                raise CfbError("broken sector chain")
            seen.add(sec)
            out.append(reader(sec))  # type: ignore[operator]
            sec = table[sec]
        return b"".join(out)

    def _entry(self, sid: int, b: bytes) -> DirEntry:
        (name_len,) = struct.unpack_from("<H", b, 64)
        name = b[: max(0, name_len - 2)].decode("utf-16-le", errors="replace")
        typ = b[66]
        left, right, child = struct.unpack_from("<III", b, 68)
        start, size_lo, size_hi = struct.unpack_from("<III", b, 116)
        size = size_lo if self.major == 3 else size_lo | (size_hi << 32)
        return DirEntry(sid, name, typ, left, right, child, start, size)

    def _walk(self) -> dict[str, DirEntry]:
        paths: dict[str, DirEntry] = {}
        visited: set[int] = set()

        def siblings(sid: int, prefix: str) -> None:
            stack = [sid]
            while stack:
                s = stack.pop()
                if s == NOSTREAM or s >= len(self.entries) or s in visited:
                    continue
                visited.add(s)
                e = self.entries[s]
                stack.extend([e.left, e.right])
                path = f"{prefix}{e.name}"
                if e.type == 2:
                    paths[path] = e
                elif e.type == 1:
                    siblings(e.child, path + "/")

        siblings(self.entries[0].child, "")
        return paths

    # -- public -----------------------------------------------------------------
    def list_streams(self) -> list[str]:
        return sorted(self.paths)

    def exists(self, path: str) -> bool:
        return path in self.paths

    def read(self, path: str) -> bytes:
        e = self.paths.get(path)
        if e is None:
            raise KeyError(path)
        if e.size < self.cutoff:
            raw = self._chain_bytes(e.start, self.minifat, self._mini)
        else:
            raw = self._chain_bytes(e.start, self.fat, self._sector)
        if len(raw) < e.size:
            raise CfbError(f"stream {path} truncated")
        return raw[: e.size]
