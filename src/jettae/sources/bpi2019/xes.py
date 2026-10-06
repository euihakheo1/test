"""Streaming XES reader (lxml.iterparse; never loads the whole log).

Attribute values are kept as strings exactly as written in the file (XES ``float`` values
are later parsed with ``Decimal`` — never ``float``). Nested attributes (lists / containers)
are ignored. ``.xes``, ``.xes.gz`` and ``.zip`` (first ``*.xes`` member) are accepted.
"""

from __future__ import annotations

import gzip
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from lxml import etree

_ATTR_TAGS = frozenset({"string", "date", "int", "float", "boolean", "id"})


@dataclass(frozen=True)
class XesEvent:
    attrs: dict[str, str]

    @property
    def activity(self) -> str:
        return self.attrs.get("concept:name", "")

    @property
    def timestamp(self) -> str:
        return self.attrs.get("time:timestamp", "")


@dataclass(frozen=True)
class XesTrace:
    attrs: dict[str, str]
    events: list[XesEvent] = field(default_factory=list)
    index: int = 0  # 0-based position in the log

    @property
    def name(self) -> str:
        return self.attrs.get("concept:name", "")


def _local(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _attrs(elem: etree._Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for child in elem:
        if _local(child.tag) in _ATTR_TAGS:
            k = child.get("key")
            v = child.get("value")
            if k is not None and v is not None:
                out[k] = v
    return out


@contextmanager
def open_log(path: Path) -> Iterator[IO[bytes]]:
    """Open an XES file (plain, gzip or zip) as a binary stream."""
    name = path.name.lower()
    if name.endswith(".gz"):
        with gzip.open(path, "rb") as gz:
            yield gz  # type: ignore[misc]
    elif name.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            members = sorted(n for n in z.namelist() if n.lower().endswith(".xes"))
            if not members:
                raise ValueError(f"no .xes member in {path}")
            with z.open(members[0]) as f:
                yield f
    else:
        with path.open("rb") as f:
            yield f


def iter_traces(path: Path, limit: int | None = None) -> Iterator[XesTrace]:
    """Yield traces one at a time; memory use is bounded by the largest trace."""
    with open_log(path) as fh:
        ctx = etree.iterparse(fh, events=("end",), huge_tree=True, recover=False)
        n = 0
        for _ev, elem in ctx:
            if _local(elem.tag) != "trace":
                continue
            events = [XesEvent(_attrs(e)) for e in elem if _local(e.tag) == "event"]
            yield XesTrace(_attrs(elem), events, n)
            n += 1
            # free memory: clear this trace and already-processed siblings
            elem.clear()
            parent = elem.getparent()
            if parent is not None:
                while elem.getprevious() is not None:
                    del parent[0]
            if limit is not None and n >= limit:
                break
        del ctx
