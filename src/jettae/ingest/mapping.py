"""Deterministic column-mapping suggestions from header synonyms.

Confidence is a *bucket* derived from how a header matched, not a probability:

- ``CONFIRMED``: chosen by the user;
- ``HIGH``: the normalised header equals a known synonym and sample values have the right type;
- ``MEDIUM``: a known synonym is contained in the header (or HIGH with weak sample values);
- ``LOW``: the header is part of a synonym, or suggested by an optional LLM suggester;
- ``NONE``: not mapped.

An optional :class:`MappingSuggester` (an adapter outside this package, e.g. an LLM) may
fill fields the rules left unmapped; its suggestions are always ``LOW`` and always require
user confirmation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol, runtime_checkable

from jettae.ingest.values import (
    ValueParseError,
    parse_amount,
    parse_brn,
    parse_date,
    parse_int,
    parse_time,
)


class Confidence(StrEnum):
    CONFIRMED = "confirmed"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


_ORDER = [Confidence.NONE, Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH, Confidence.CONFIRMED]


def bucket_rank(c: Confidence) -> int:
    return _ORDER.index(c)


def _downgrade(c: Confidence) -> Confidence:
    if c in (Confidence.CONFIRMED, Confidence.NONE, Confidence.LOW):
        return c
    return _ORDER[_ORDER.index(c) - 1]


class FieldKind(StrEnum):
    DATE = "date"
    TIME = "time"
    AMOUNT = "amount"
    TEXT = "text"
    ID = "id"
    BRN = "brn"
    INT = "int"


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str  # Korean label shown to the user
    kind: FieldKind
    synonyms: tuple[str, ...]
    required: bool = False
    exclude: tuple[str, ...] = ()  # normalised substrings that disqualify a header


_UNIT = re.compile(r"\((원|krw|₩|건|일|금액|단위:?원)\)|\[(원|krw)\]", re.I)
_PUNCT = re.compile(r"[\s 　·ㆍ•/\\_\-.:,()\[\]{}<>*#'\"]+")


def normalize_header(h: str) -> str:
    s = (h or "").strip().lower()
    s = _UNIT.sub("", s)
    return _PUNCT.sub("", s)


@dataclass(frozen=True)
class ColumnMatch:
    field: str
    column: int  # 0-based column index in the table view
    header: str
    confidence: Confidence
    score: int
    reason: str


@dataclass(frozen=True)
class MappingSuggestion:
    format_id: str
    matches: tuple[ColumnMatch, ...]
    unmapped_required: tuple[str, ...]
    unmapped_columns: tuple[int, ...]
    sources: tuple[str, ...] = ("rules",)

    def column_of(self, field_name: str) -> int | None:
        for m in self.matches:
            if m.field == field_name:
                return m.column
        return None

    def match(self, field_name: str) -> ColumnMatch | None:
        for m in self.matches:
            if m.field == field_name:
                return m
        return None

    @property
    def needs_confirmation(self) -> bool:
        if self.unmapped_required:
            return True
        return any(
            m.confidence not in (Confidence.HIGH, Confidence.CONFIRMED)
            for m in self.matches
            if m.field in self._required
        )

    _required: frozenset[str] = field(default=frozenset(), compare=False, repr=False)

    @property
    def overall(self) -> Confidence:
        req = [m for m in self.matches if m.field in self._required]
        if self.unmapped_required or not req:
            return Confidence.NONE if not self.matches else Confidence.LOW
        return min((m.confidence for m in req), key=bucket_rank)

    def as_dict(self) -> dict[str, object]:
        return {
            "format_id": self.format_id,
            "needs_confirmation": self.needs_confirmation,
            "overall": self.overall.value,
            "matches": [
                {
                    "field": m.field,
                    "column": m.column,
                    "header": m.header,
                    "confidence": m.confidence.value,
                    "reason": m.reason,
                }
                for m in self.matches
            ],
            "unmapped_required": list(self.unmapped_required),
            "unmapped_columns": list(self.unmapped_columns),
            "sources": list(self.sources),
        }


@runtime_checkable
class MappingSuggester(Protocol):
    """Optional suggester (e.g. an LLM). Returns ``{field_name: column_index}`` proposals only;
    the deterministic rules and the user decide. Implementations live outside ``ingest``."""

    name: str

    def suggest(
        self,
        headers: Sequence[str],
        samples: Sequence[Sequence[str]],
        fields: Sequence[FieldSpec],
    ) -> Mapping[str, int]: ...


def header_score(header: str, spec: FieldSpec) -> tuple[int, Confidence, str]:
    """Score how well ``header`` names ``spec``. Deterministic; 0 means no match."""
    h = normalize_header(header)
    if not h:
        return 0, Confidence.NONE, ""
    if any(x in h for x in spec.exclude):
        return 0, Confidence.NONE, ""
    best: tuple[int, Confidence, str] = (0, Confidence.NONE, "")
    for syn in spec.synonyms:
        s = normalize_header(syn)
        if not s:
            continue
        if h == s:
            cand = (100, Confidence.HIGH, f"header equals synonym {syn!r}")
        elif len(s) >= 2 and s in h:
            cand = (60 + min(len(s), 20), Confidence.MEDIUM, f"header contains {syn!r}")
        elif len(h) >= 2 and h in s:
            cand = (30 + min(len(h), 20), Confidence.LOW, f"header is part of {syn!r}")
        else:
            continue
        if cand[0] > best[0]:
            best = cand
    return best


def _value_ok(kind: FieldKind, text: str) -> bool:
    try:
        if kind is FieldKind.DATE:
            return parse_date(text, allow_serial=True) is not None
        if kind is FieldKind.AMOUNT:
            return parse_amount(text) is not None
        if kind is FieldKind.TIME:
            return parse_time(text) is not None
        if kind is FieldKind.INT:
            return parse_int(text) is not None
        if kind is FieldKind.BRN:
            b = parse_brn(text)
            return b is not None and bool(re.search(r"\d", b))
    except ValueParseError:
        return False
    return True


def sample_check(kind: FieldKind, values: Sequence[str]) -> float | None:
    """Fraction of non-empty sample values parseable as ``kind`` (None when no samples)."""
    vals = [v for v in values if v and v.strip()]
    if not vals:
        return None
    return sum(1 for v in vals if _value_ok(kind, v)) / len(vals)


def suggest_mapping(
    format_id: str,
    headers: Sequence[str],
    samples: Sequence[Sequence[str]],
    fields: Sequence[FieldSpec],
    *,
    suggester: MappingSuggester | None = None,
) -> MappingSuggestion:
    """Greedy, deterministic assignment of columns to fields by header score."""
    specs = {f.name: f for f in fields}
    cands: list[tuple[int, int, int, str, Confidence, str]] = []
    for col, header in enumerate(headers):
        for spec in fields:
            score, conf, reason = header_score(header, spec)
            if score <= 0:
                continue
            cands.append((-score, 0 if spec.required else 1, col, spec.name, conf, reason))
    cands.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
    used_cols: set[int] = set()
    matches: dict[str, ColumnMatch] = {}
    for neg, _req, col, name, conf, reason in cands:
        if name in matches or col in used_cols:
            continue
        col_vals = [row[col] if col < len(row) else "" for row in samples]
        frac = sample_check(specs[name].kind, col_vals)
        if frac is not None and frac < 0.6:
            if conf is Confidence.LOW:
                continue  # weak header + wrong-looking values: not a match
            conf = _downgrade(conf)
            reason += f"; only {round(frac * 100)}% of sample values look like {specs[name].kind}"
        matches[name] = ColumnMatch(name, col, headers[col], conf, -neg, reason)
        used_cols.add(col)
    sources = ["rules"]
    if suggester is not None:
        missing = [f for f in fields if f.name not in matches]
        if missing:
            try:
                proposal = dict(
                    suggester.suggest(list(headers), [list(r) for r in samples], missing)
                )
            except Exception as e:  # suggester problems never break the deterministic path
                proposal = {}
                sources.append(f"suggester_error:{type(e).__name__}")
            for name in sorted(proposal):
                col = proposal[name]
                if name not in specs or name in matches or not isinstance(col, int):
                    continue
                if col in used_cols or not (0 <= col < len(headers)):
                    continue
                frac = sample_check(
                    specs[name].kind, [r[col] if col < len(r) else "" for r in samples]
                )
                if frac is not None and frac < 0.6:
                    continue
                matches[name] = ColumnMatch(
                    name, col, headers[col], Confidence.LOW, 0, f"suggested by {suggester.name}"
                )
                used_cols.add(col)
            sources.append(f"suggester:{suggester.name}")
    ordered = tuple(sorted(matches.values(), key=lambda m: m.column))
    required = frozenset(f.name for f in fields if f.required)
    return MappingSuggestion(
        format_id=format_id,
        matches=ordered,
        unmapped_required=tuple(f.name for f in fields if f.required and f.name not in matches),
        unmapped_columns=tuple(
            c for c in range(len(headers)) if c not in used_cols and headers[c].strip()
        ),
        sources=tuple(sources),
        _required=required,
    )


def confirm_mapping(
    suggestion: MappingSuggestion,
    headers: Sequence[str],
    overrides: Mapping[str, int | str | None],
    fields: Sequence[FieldSpec],
) -> MappingSuggestion:
    """Apply a user's mapping. Values are a column index, a header text, or ``None`` (unmap).
    Fields named in ``overrides`` become ``CONFIRMED``; others keep their suggestion."""
    specs = {f.name: f for f in fields}
    by_field = {m.field: m for m in suggestion.matches}
    norm_headers = [normalize_header(h) for h in headers]
    for name, target in overrides.items():
        if name not in specs:
            raise ValueError(f"unknown field {name!r}")
        if target is None:
            by_field.pop(name, None)
            continue
        if isinstance(target, str):
            nt = normalize_header(target)
            if nt not in norm_headers:
                raise ValueError(f"no column with header {target!r}")
            col = norm_headers.index(nt)
        else:
            col = int(target)
            if not 0 <= col < len(headers):
                raise ValueError(f"column {col} out of range")
        for other in [k for k, m in by_field.items() if m.column == col and k != name]:
            by_field.pop(other)
        by_field[name] = ColumnMatch(name, col, headers[col], Confidence.CONFIRMED, 100, "user")
    used = {m.column for m in by_field.values()}
    required = frozenset(f.name for f in fields if f.required)
    return replace(
        suggestion,
        matches=tuple(sorted(by_field.values(), key=lambda m: m.column)),
        unmapped_required=tuple(f.name for f in fields if f.required and f.name not in by_field),
        unmapped_columns=tuple(
            c for c in range(len(headers)) if c not in used and headers[c].strip()
        ),
        sources=(*suggestion.sources, "user"),
        _required=required,
    )
