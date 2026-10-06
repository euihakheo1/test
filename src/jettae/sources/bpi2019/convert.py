"""BPI Challenge 2019 XES -> one compact record per purchase-order item (JSONL, gzip).

Each record keeps the trace attributes and, per event, ``[activity, timestamp, amount]``
where ``amount`` is the original ``Cumulative net worth (EUR)`` string (lossless; parsed
later with ``Decimal``). Nothing is filtered or invented: every trace becomes one record.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from jettae.domain.money import Money, RoundingMode
from jettae.sources.bpi2019.xes import XesTrace, iter_traces

AMOUNT_KEY = "Cumulative net worth (EUR)"

ACT_CREATE = "Create Purchase Order Item"
ACT_GR = "Record Goods Receipt"
ACT_GR_CANCEL = "Cancel Goods Receipt"
ACT_IR = "Record Invoice Receipt"
ACT_IR_CANCEL = "Cancel Invoice Receipt"
ACT_CLEAR = "Clear Invoice"

# trace attribute keys used by the analysis (all other keys are kept verbatim as well)
KEY_VENDOR = "Vendor"
KEY_CATEGORY = "Item Category"
KEY_GR_BASED = "GR-Based Inv. Verif."
KEY_GR_FLAG = "Goods Receipt"


@dataclass(frozen=True)
class EventRec:
    activity: str
    timestamp: str
    amount: str | None

    @property
    def when(self) -> datetime | None:
        return parse_ts(self.timestamp)

    @property
    def day(self) -> date | None:
        """Business date = calendar date in the timestamp's own UTC offset."""
        dt = self.when
        return dt.date() if dt is not None else None


@dataclass(frozen=True)
class PoItem:
    id: str
    attrs: dict[str, str]
    events: tuple[EventRec, ...]

    @property
    def vendor(self) -> str:
        return self.attrs.get(KEY_VENDOR, "")

    @property
    def category(self) -> str:
        return self.attrs.get(KEY_CATEGORY, "(none)")

    def of(self, activity: str) -> list[EventRec]:
        return [e for e in self.events if e.activity == activity]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "attrs": self.attrs,
            "events": [[e.activity, e.timestamp, e.amount] for e in self.events],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> PoItem:
        return cls(
            d["id"],
            dict(d.get("attrs", {})),
            tuple(EventRec(a, t, m) for a, t, m in d.get("events", [])),
        )


def parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def amount_minor(value: str | None) -> tuple[Money | None, bool]:
    """EUR string -> (Money in cents, rounded?). Uses Decimal; HALF_UP to whole cents."""
    if value is None or value == "":
        return None, False
    try:
        d = Decimal(value)
    except InvalidOperation:
        return None, False
    if not d.is_finite():
        return None, False
    cents = d * 100
    rounded = cents != cents.to_integral_value()
    return Money.from_decimal(cents, RoundingMode.HALF_UP, "EUR"), rounded


def item_from_trace(tr: XesTrace) -> PoItem:
    evs = tuple(EventRec(e.activity, e.timestamp, e.attrs.get(AMOUNT_KEY)) for e in tr.events)
    name = tr.name or f"trace#{tr.index}"
    return PoItem(name, dict(tr.attrs), evs)


def iter_items_from_xes(path: Path, limit: int | None = None) -> Iterator[PoItem]:
    for tr in iter_traces(path, limit=limit):
        yield item_from_trace(tr)


def write_items(items: Iterable[PoItem], out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    tmp = out.with_suffix(out.suffix + ".part")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="\n") as f:
        for it in items:
            f.write(json.dumps(it.to_json(), ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
            n += 1
    tmp.replace(out)
    return n


def read_items(path: Path, limit: int | None = None) -> Iterator[PoItem]:
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:  # type: ignore[operator]
        for i, line in enumerate(f):
            if limit is not None and i >= limit:
                break
            line = line.strip()
            if line:
                yield PoItem.from_json(json.loads(line))


def iter_items(path: Path, limit: int | None = None) -> Iterator[PoItem]:
    """Items from an XES log or from a converted ``*.jsonl[.gz]`` file."""
    name = path.name.lower()
    if name.endswith((".jsonl", ".jsonl.gz")):
        return read_items(path, limit)
    return iter_items_from_xes(path, limit)
