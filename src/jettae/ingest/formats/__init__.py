"""Format recognizers: 홈택스 세금계산서 목록, 국내 은행 거래내역, 정산서, 약정 조건표."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from jettae.ingest.cells import Table
from jettae.ingest.formats.bank import BANK
from jettae.ingest.formats.base import FormatSpec
from jettae.ingest.formats.hometax import HOMETAX
from jettae.ingest.formats.settle import AGREEMENT, SETTLEMENT
from jettae.ingest.mapping import Confidence, MappingSuggester, MappingSuggestion, suggest_mapping
from jettae.ingest.table import TableView, detect_header

FORMATS: tuple[FormatSpec, ...] = (HOMETAX, BANK, SETTLEMENT, AGREEMENT)
FORMATS_BY_ID = {f.id: f for f in FORMATS}
_WEIGHT = {Confidence.CONFIRMED: 3, Confidence.HIGH: 3, Confidence.MEDIUM: 2, Confidence.LOW: 1}
MIN_SCORE = 6  # below this a table is not considered to be of any known format


def vocabulary(formats: Iterable[FormatSpec] = FORMATS) -> list[str]:
    out: list[str] = []
    for f in formats:
        for fs in f.fields:
            out.extend(fs.synonyms)
        out.extend(f.markers)
    return out


@dataclass(frozen=True)
class Candidate:
    spec: FormatSpec
    headers: tuple[str, ...]  # after format-specific contextualisation
    mapping: MappingSuggestion
    score: int
    complete: bool


@dataclass(frozen=True)
class Recognition:
    view: TableView
    best: Candidate | None
    candidates: tuple[Candidate, ...]

    @property
    def recognized(self) -> bool:
        return self.best is not None and self.best.complete and self.best.score >= MIN_SCORE


def score_format(
    spec: FormatSpec, view: TableView, suggester: MappingSuggester | None = None
) -> Candidate:
    headers = tuple(spec.contextualize(view.headers) if spec.contextualize else view.headers)
    m = suggest_mapping(spec.id, headers, view.samples(), spec.fields, suggester=suggester)
    score = sum(_WEIGHT.get(x.confidence, 0) for x in m.matches) + 2 * spec.marker_hits(headers)
    return Candidate(spec, headers, m, score, spec.complete(m))


def recognize_table(
    table: Table,
    *,
    format_id: str | None = None,
    suggester: MappingSuggester | None = None,
) -> Recognition:
    view = detect_header(table, vocabulary())
    if not view.headers:
        return Recognition(view, None, ())
    specs = [FORMATS_BY_ID[format_id]] if format_id else list(FORMATS)
    cands = [score_format(s, view, suggester) for s in specs]
    order = {s.id: i for i, s in enumerate(FORMATS)}
    cands.sort(key=lambda c: (not c.complete, -c.score, order[c.spec.id]))
    best = cands[0] if cands and (cands[0].score > 0 or format_id) else None
    return Recognition(view, best, tuple(cands))


__all__ = [
    "AGREEMENT",
    "BANK",
    "FORMATS",
    "FORMATS_BY_ID",
    "HOMETAX",
    "SETTLEMENT",
    "Candidate",
    "FormatSpec",
    "Recognition",
    "recognize_table",
    "score_format",
    "vocabulary",
]
