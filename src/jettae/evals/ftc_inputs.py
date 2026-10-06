"""FTC evaluation inputs: seed CSV/JSONL loading, cell parsing, variant labels.

Files (real-data derived, with provenance; see ``data/seeds/*.README.md``):
- ``data/seeds/ftc_rows.csv``   -- rows transcribed from 의결서 table images (flSeq per row)
- ``data/seeds/ftc_tables.csv`` -- per-table metadata (column semantics, elided rows, 합계)
- ``data/seeds/ftc_case_facts.jsonl`` -- case-level regex facts from the decision text
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from jettae.domain.money import RoundingMode
from jettae.domain.status import TradeType
from jettae.sources.ftc.parse import unit_multiplier
from jettae.sources.ftc.paths import long_path, seeds_dir

ROW_COLUMNS = (
    "decision_id",
    "case_no",
    "flseq",
    "table_label",
    "row_idx",
    "deal_type",
    "base_date_kind",
    "base_date",
    "due_date_in_table",
    "paid_date",
    "principal_krw",
    "delay_days_in_table",
    "interest_in_table",
    "unpaid_interest_in_table",
    "unit_note",
    "transcriber",
    "verified",
    "notes",
    "serial_no",
)
TABLE_COLUMNS = (
    "decision_id",
    "case_no",
    "flseq",
    "table_label",
    "caption",
    "unit_note",
    "deal_type",
    "base_col",
    "due_col",
    "due_col_semantics",
    "paid_col",
    "principal_col",
    "interest_col",
    "unpaid_interest_col",
    "rows_visible",
    "rows_elided",
    "printed_total_principal",
    "printed_total_interest",
    "printed_total_unpaid_interest",
    "serial_unit",
    "notes",
)
VARIANTS: tuple[tuple[bool, RoundingMode], ...] = tuple(
    (r, m) for r in (False, True) for m in (RoundingMode.FLOOR, RoundingMode.HALF_UP)
)
_RANGE_SEP = re.compile(r"\s*[~～∼]\s*")


def variant_label(rollover: bool, rounding: RoundingMode) -> str:
    return f"rollover_{'on' if rollover else 'off'}/{rounding.value}"


# ------------------------------------------------------------------ parsing helpers
def parse_date_cell(value: str) -> date | None:
    v = (value or "").strip()
    if not v or v in {"-", "–"}:
        return None
    return date.fromisoformat(v)


def parse_range(value: str) -> tuple[str, str] | None:
    v = (value or "").strip()
    parts = _RANGE_SEP.split(v)
    return (parts[0], parts[1]) if len(parts) == 2 else None


def parse_int_cell(value: str) -> int | None:
    v = (value or "").strip().replace(",", "")
    if not v or v in {"-", "–"}:
        return None
    if not re.fullmatch(r"-?\d+", v):
        raise ValueError(f"not an integer cell: {value!r}")
    return int(v)


def is_uncertain(notes: str) -> bool:
    return "UNCERTAIN" in (notes or "")


@dataclass(frozen=True)
class SeedRow:
    raw: dict[str, str]

    def __getitem__(self, k: str) -> str:
        return (self.raw.get(k) or "").strip()

    @property
    def key(self) -> str:
        return f"{self['decision_id']}/{self['flseq']}/{self['row_idx']}"

    @property
    def table_key(self) -> tuple[str, str]:
        return (self["decision_id"], self["flseq"])

    @property
    def is_range(self) -> bool:
        return any(parse_range(self[c]) for c in ("base_date", "due_date_in_table"))

    @property
    def trade_type(self) -> TradeType | None:
        v = self["deal_type"]
        return TradeType(v) if v else None

    @property
    def multiplier(self) -> int:
        return unit_multiplier(self["unit_note"] and f"단위: {self['unit_note']}")


@dataclass(frozen=True)
class TableMeta:
    raw: dict[str, str]

    def __getitem__(self, k: str) -> str:
        return (self.raw.get(k) or "").strip()

    @property
    def key(self) -> tuple[str, str]:
        return (self["decision_id"], self["flseq"])

    @property
    def complete(self) -> bool:
        return self["rows_elided"].lower() == "no"

    @property
    def multiplier(self) -> int:
        return unit_multiplier(self["unit_note"] and f"단위: {self['unit_note']}")


def read_csv(path: Path, required: Iterable[str]) -> list[dict[str, str]]:
    with long_path(path).open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name}: missing columns {missing}")
        return [dict(r) for r in reader]


def load_rows(path: Path | None = None) -> list[SeedRow]:
    return [SeedRow(r) for r in read_csv(path or seeds_dir() / "ftc_rows.csv", ROW_COLUMNS)]


def load_tables(path: Path | None = None) -> dict[tuple[str, str], TableMeta]:
    rows = read_csv(path or seeds_dir() / "ftc_tables.csv", TABLE_COLUMNS)
    return {TableMeta(r).key: TableMeta(r) for r in rows}


def load_facts(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or seeds_dir() / "ftc_case_facts.jsonl"
    if not long_path(p).exists():
        return []
    with long_path(p).open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def stated_rates(facts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per decision: the legal delay-interest rate stated in the text (most frequent, high conf)."""
    by: dict[str, Counter[str]] = defaultdict(Counter)
    where: dict[tuple[str, str], dict[str, Any]] = {}
    for f in facts:
        if f["kind"] == "legal_interest_rate" and f["confidence"] == "high" and not f["masked"]:
            by[f["decision_id"]][f["value"]] += 1
            where.setdefault(
                (f["decision_id"], f["value"]),
                {k: f[k] for k in ("section", "char_start", "char_end", "raw")},
            )
    out = {}
    for did, cnt in by.items():
        val, n = cnt.most_common(1)[0]
        out[did] = {"percent": val, "mentions": n, "span": where[(did, val)]}
    return out
