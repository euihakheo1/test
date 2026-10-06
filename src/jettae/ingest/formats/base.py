"""Format specs and the row → domain record machinery shared by all recognizers.

Every value placed on a record is read through :class:`RowReader`, which creates a
:class:`~jettae.domain.models.Fact` with a :class:`SourceSpan` (doc version + cell locator +
verbatim cell text). Fields that are not in the file stay ``None`` and are listed in the
record's ``missing`` set; base dates are only ever read from their own columns.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from typing import Any

from jettae.domain.models import Fact, SourceSpan
from jettae.domain.money import Money
from jettae.domain.status import DocKind, TradeType
from jettae.ingest.cells import Cell, Row
from jettae.ingest.mapping import FieldKind, FieldSpec, MappingSuggestion, normalize_header
from jettae.ingest.table import TableView
from jettae.ingest.values import (
    ValueParseError,
    parse_amount,
    parse_brn,
    parse_date,
    parse_id,
    parse_int,
    parse_text,
    parse_time,
)

BASE_DATE_FIELDS = ("goods_received_date", "sales_close_date")


@dataclass(frozen=True)
class IngestOptions:
    """Caller-supplied context. Nothing here is inferred from the file contents."""

    # Document-level values typed by the user. They are *values*, never column references
    # (a column for the counterparty is ``mapping[table]["counterparty"]``).
    counterparty_override: str | None = None  # company applied to every row (wins over column)
    self_brn: str | None = None  # the tenant's 사업자등록번호 (sales vs purchase invoices)
    direction: str | None = None  # "sales" | "purchase" for tax-invoice lists
    account_override: str | None = None  # bank account label (wins over the preamble)
    currency: str = "KRW"
    accept_suggested: bool = False  # build records from an unconfirmed mapping suggestion
    format_id: str | None = None  # force a format
    mapping: Mapping[str, Mapping[str, int | str | None]] = field(default_factory=dict)
    # ^ per-table user mapping: {table_name or "*": {field: column index | header | None}}


@dataclass(frozen=True)
class BuildContext:
    tenant_id: str
    doc_version_id: str
    document_key: str  # stable across versions of the same document (record ids)
    observed_at: datetime
    options: IngestOptions
    epoch_1904: bool = False
    doc_hint: str = ""  # preamble + sheet name + filename (for direction/bank hints)


@dataclass(frozen=True)
class RowIssue:
    """A problem in one source row. ``kind="excluded"``: the row produced no record;
    ``kind="value"``: the record was built but this cell was unreadable and left empty."""

    table: str
    row: int
    field: str | None
    message: str
    kind: str = "excluded"


@dataclass(frozen=True)
class TotalCheck:
    table: str
    row: int
    field: str
    stated: int
    computed: int

    @property
    def matches(self) -> bool:
        return self.stated == self.computed


@dataclass
class TableExtract:
    table: str
    format_id: str
    records: list[Any] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    issues: list[RowIssue] = field(default_factory=list)
    totals: list[TotalCheck] = field(default_factory=list)
    source_rows: int = 0  # data rows of the table (header/blank/합계 rows are not data rows)
    skipped_rows: int = 0  # data rows that produced no record (each has >= 1 issue)
    notes: list[str] = field(default_factory=list)


class RowReader:
    """Typed, fact-producing access to one mapped row."""

    def __init__(
        self,
        ctx: BuildContext,
        spec: FormatSpec,
        view: TableView,
        mapping: MappingSuggestion,
        row: Row,
        entity: str,
    ) -> None:
        self.ctx, self.spec, self.view, self.mapping, self.row = ctx, spec, view, mapping, row
        self.entity = entity
        self.subject_id = ""
        self.facts: list[Fact] = []
        self.issues: list[RowIssue] = []
        self._fields = {f.name: f for f in spec.fields}

    # ------------------------------------------------------------ raw access
    def cell(self, name: str) -> Cell | None:
        col = self.mapping.column_of(name)
        if col is None:
            return None
        c = self.row.cell(col)
        return c if c is not None and not c.is_empty else None

    def has(self, name: str) -> bool:
        return self.cell(name) is not None

    def raw_text(self, name: str) -> str:
        c = self.cell(name)
        return c.text.strip() if c else ""

    # ------------------------------------------------------------ facts
    def _locator(self, c: Cell) -> dict[str, Any]:
        loc = dict(c.locator)
        loc["table_name"] = self.view.table.name
        return loc

    def add_fact(self, name: str, value: Any, c: Cell | None, *, note: str | None = None) -> str:
        if not self.subject_id:
            raise RuntimeError("subject_id must be set before facts are created")
        fid = f"fact:{self.subject_id}:{name}"
        span = None
        if c is not None:
            loc = self._locator(c)
            hdr_col = self.mapping.column_of(name)
            if hdr_col is not None and hdr_col < len(self.view.headers):
                loc["header"] = self.view.headers[hdr_col]
            if note:
                loc["note"] = note
            span = SourceSpan(self.ctx.doc_version_id, loc, c.text)
        self.facts.append(
            Fact(
                id=fid,
                tenant_id=self.ctx.tenant_id,
                kind=f"{self.entity}.{name}",
                value=value,
                span=span,
                extractor=f"ingest.{self.spec.id}@{self.spec.version}",
                observed_at=self.ctx.observed_at,
                subject_id=self.subject_id,
            )
        )
        return fid

    def _issue(self, name: str | None, msg: str) -> None:
        self.issues.append(RowIssue(self.view.table.name, self.row.index, name, msg))

    # ------------------------------------------------------------ typed getters
    def date(self, name: str) -> date | None:
        c = self.cell(name)
        if c is None:
            return None
        try:
            dv = parse_date(c, allow_serial=True, epoch_1904=self.ctx.epoch_1904)
        except ValueParseError as e:
            self._issue(name, str(e))
            return None
        if dv is None:
            return None
        self.add_fact(name, dv.value, c, note=dv.note)
        if dv.time is not None:
            self.add_fact(name + "_time", dv.time.isoformat(), c)
        return dv.value

    def time(self, name: str) -> time | None:
        c = self.cell(name)
        if c is None:
            return None
        try:
            t = parse_time(c)
        except ValueParseError as e:
            self._issue(name, str(e))
            return None
        if t is not None:
            self.add_fact(name, t.isoformat(), c)
        return t

    def amount(self, name: str) -> int | None:
        c = self.cell(name)
        if c is None:
            return None
        try:
            v = parse_amount(c)
        except ValueParseError as e:
            self._issue(name, str(e))
            return None
        if v is not None:
            self.add_fact(name, Money(v, self.ctx.options.currency), c)
        return v

    def text(self, name: str) -> str | None:
        c = self.cell(name)
        v = parse_text(c) if c else None
        if v is not None:
            self.add_fact(name, v, c)
        return v

    def ident(self, name: str) -> str | None:
        c = self.cell(name)
        v = parse_id(c) if c else None
        if v is not None:
            self.add_fact(name, v, c)
        return v

    def brn(self, name: str) -> str | None:
        c = self.cell(name)
        v = parse_brn(c) if c else None
        if v is not None:
            self.add_fact(name, v, c)
        return v

    def integer(self, name: str) -> int | None:
        c = self.cell(name)
        if c is None:
            return None
        try:
            v = parse_int(c)
        except ValueParseError as e:
            self._issue(name, str(e))
            return None
        if v is not None:
            self.add_fact(name, v, c)
        return v

    def read_all_other(self, skip: set[str]) -> None:
        """Keep every other mapped column as a text fact (provenance for later review)."""
        for m in self.mapping.matches:
            if (
                m.field in skip
                or any(f.id == f"fact:{self.subject_id}:{m.field}" for f in self.facts)
                or any(i.field == m.field for i in self.issues)  # already reported once
            ):
                continue
            spec = self._fields.get(m.field)
            if spec is None:
                continue
            if spec.kind is FieldKind.AMOUNT:
                self.amount(m.field)
            elif spec.kind is FieldKind.DATE:
                self.date(m.field)
            elif spec.kind is FieldKind.BRN:
                self.brn(m.field)
            elif spec.kind is FieldKind.ID:
                self.ident(m.field)
            else:
                self.text(m.field)


RowBuilder = Callable[[RowReader], Any | None]


@dataclass(frozen=True)
class FormatSpec:
    id: str
    title: str
    doc_kind: DocKind
    entity: str  # ledger entity name produced (invoice, bank_txn, settlement_line, agreement)
    id_prefix: str
    fields: tuple[FieldSpec, ...]
    markers: tuple[str, ...]  # headers that strongly indicate this format
    build: RowBuilder
    key_fields: tuple[str, ...]  # fields forming a natural row key (ids stable across versions)
    required_any: tuple[tuple[str, ...], ...] = ()
    contextualize: Callable[[Sequence[str]], list[str]] | None = None
    version: str = "1"
    sources: tuple[str, ...] = ()
    verification: str = ""  # how the header list was verified (and what was not)
    total_fields: tuple[str, ...] = ()  # amount fields checked against 합계 rows

    def complete(self, m: MappingSuggestion) -> bool:
        mapped = {x.field for x in m.matches}
        if m.unmapped_required:
            return False
        return all(any(f in mapped for f in grp) for grp in self.required_any)

    def marker_hits(self, headers: Sequence[str]) -> int:
        norm = {normalize_header(h) for h in headers}
        return sum(1 for mk in self.markers if normalize_header(mk) in norm)


def row_key(view: TableView, mapping: MappingSuggestion, row: Row, fields: Sequence[str]) -> str:
    parts = []
    for f in fields:
        col = mapping.column_of(f)
        c = row.cell(col) if col is not None else None
        parts.append(c.text.strip() if c else "")
    if not any(parts):
        parts = [c.text.strip() for c in row.cells]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def extract_table(
    ctx: BuildContext, spec: FormatSpec, view: TableView, mapping: MappingSuggestion
) -> TableExtract:
    out = TableExtract(view.table.name, spec.id)
    seen: Counter[str] = Counter()
    for row in view.data:
        reader = RowReader(ctx, spec, view, mapping, row, spec.entity)
        key = row_key(view, mapping, row, spec.key_fields)
        seen[key] += 1
        reader.subject_id = f"{spec.id_prefix}:{ctx.document_key}:{key}#{seen[key]}"
        try:
            rec = spec.build(reader)
        except ValueParseError as e:  # pragma: no cover - getters catch these
            reader._issue(None, str(e))
            rec = None
        out.source_rows += 1
        if rec is None:
            # Every excluded row must be visible: a builder may decline a row without saying
            # why (e.g. an amount cell that parses to "no value"), so add a generic issue.
            if not reader.issues:
                reader._issue(None, "행을 읽지 못해 거래로 반영하지 않음(필수 값 없음)")
            out.issues.extend(replace(i, kind="excluded") for i in reader.issues)
            out.skipped_rows += 1
            continue
        out.issues.extend(replace(i, kind="value") for i in reader.issues)
        out.records.append(rec)
        out.facts.extend(reader.facts)
    # 합계 rows: compare stated totals with the sum of extracted data rows (informational)
    for trow in view.totals:
        for f in spec.total_fields:
            col = mapping.column_of(f)
            c = trow.cell(col) if col is not None else None
            if c is None or c.is_empty:
                continue
            try:
                stated = parse_amount(c)
            except ValueParseError:
                continue
            if stated is None:
                continue
            computed = 0
            for r in view.data:
                rc = r.cell(col) if col is not None else None
                try:
                    computed += (parse_amount(rc) or 0) if rc is not None else 0
                except ValueParseError:
                    continue
            out.totals.append(TotalCheck(view.table.name, trow.index, f, stated, computed))
    return out


# ------------------------------------------------------------------ shared value maps
def trade_type_from_text(s: str | None) -> TradeType | None:
    if not s:
        return None
    n = normalize_header(s)
    if "직매입" in n:
        return TradeType.DIRECT
    if any(k in n for k in ("특약매입", "특정매입", "위수탁", "위탁판매", "수탁", "임대을")):
        return TradeType.CONSIGNMENT
    if "하도급" in n:
        return TradeType.SUBCONTRACT
    return None


def missing_base_fields(
    trade_type: TradeType | None, goods_received: date | None, sales_close: date | None
) -> set[str]:
    missing: set[str] = set()
    if trade_type is None:
        missing.add("trade_type")
        if goods_received is None:
            missing.add("goods_received_date")
        if sales_close is None:
            missing.add("sales_close_date")
    elif trade_type in (TradeType.DIRECT, TradeType.SUBCONTRACT) and goods_received is None:
        missing.add("goods_received_date")
    elif trade_type is TradeType.CONSIGNMENT and sales_close is None:
        missing.add("sales_close_date")
    return missing
