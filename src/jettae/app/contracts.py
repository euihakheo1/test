"""Strict cross-boundary contracts for document ingestion (Pydantic v2).

These models are shared by the HTTP API, the job worker, the ingestion package and the CLI,
so the same rules hold on every path:

* :class:`MappingRequest` - a user's column mapping. ``columns`` maps a *field name* to a
  0-based *column index* (or ``null`` = the user removed that field's column). ``options``
  holds document-level values typed by the user. The two never share keys: a column for
  the counterparty is ``columns.counterparty``; a company name applied to every row is
  ``options.counterparty_override``. (The retired flat shape ``{"counterparty": 1}`` was
  ambiguous - an index could become the company name - and is rejected.)
* :class:`ParseResult` - what a parser returns for one document version: records, facts,
  row-level issues, table total checks and row accounting (:class:`RowCounts`).
* :class:`ApplyOutcome` / :class:`IngestJobResult` - what applying a parse result did to the
  current ledger (see :mod:`jettae.app.doc_apply` for the rules).

The module depends on pydantic and the pure domain only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from jettae.domain.models import Agreement, BankTxn, Fact, Invoice, SettlementLine
from jettae.domain.status import DocumentStatus

# Format ids known to the ingestion package (checked against ``ingest.formats.FORMATS``
# by a test, so the two lists cannot drift apart silently).
FormatId = Literal["hometax_etax_list", "kr_bank_txn", "retail_settlement", "agreement_terms"]
FORMAT_IDS: tuple[str, ...] = (
    "hometax_etax_list",
    "kr_bank_txn",
    "retail_settlement",
    "agreement_terms",
)

_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

ColumnIndex = Annotated[StrictInt, Field(ge=0, le=10_000)]
OptionText = Annotated[StrictStr, Field(min_length=1, max_length=200)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class MappingOptions(_Strict):
    """Document-level values typed by the user (never column references)."""

    counterparty_override: OptionText | None = Field(
        None,
        description=(
            "거래처 이름을 모든 행에 적용(열 매핑보다 우선). 문서에 거래처 열이 없을 때 쓴다."
        ),
    )
    account_override: OptionText | None = Field(
        None, description="은행 계좌 이름을 모든 행에 적용(문서 머리말의 계좌보다 우선)."
    )
    self_brn: Annotated[StrictStr, Field(pattern=r"^[0-9]{3}-?[0-9]{2}-?[0-9]{5}$")] | None = Field(
        None, description="우리 회사 사업자등록번호(세금계산서 매출·매입 구분)"
    )
    direction: Literal["sales", "purchase"] | None = None
    accept_suggested: StrictBool = Field(
        False, description="확인되지 않은 열 매핑 제안을 그대로 사용(사용자가 명시한 경우만)"
    )

    @field_validator("counterparty_override", "account_override")
    @classmethod
    def _clean_text(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("must not be blank")
        if _CONTROL.search(s):
            raise ValueError("control characters are not allowed")
        return s


class MappingRequest(_Strict):
    """A confirmed column mapping:
    ``{"format_id"?, "table"?, "columns": {field: index|null}, "options"}``.

    ``table`` names the table (sheet / PDF table / ``csv``) whose columns ``columns`` refers
    to - the ``table`` of the mapping suggestion. Column indexes only mean something for one
    header row, so the mapping is applied to that table (and to other not-yet-recognised
    tables with the identical header row), never to a table recognised with another column
    order. Without ``table`` the mapping is accepted only when it cannot be misapplied (one
    table, or all unrecognised tables share one header row); otherwise parsing fails with
    :class:`MappingContractError` (see ``jettae.ingest.pipeline._mapping_targets``)."""

    format_id: FormatId | None = None
    table: Annotated[StrictStr, Field(min_length=1, max_length=200)] | None = None
    columns: dict[str, ColumnIndex | None] = Field(
        default_factory=dict,
        description="field name -> 0-based column index; null = the user removed the column",
    )
    options: MappingOptions = Field(default_factory=lambda: MappingOptions.model_validate({}))

    @field_validator("columns")
    @classmethod
    def _field_names(cls, v: dict[str, int | None]) -> dict[str, int | None]:
        if len(v) > 200:
            raise ValueError("too many fields")
        for k in v:
            if not _FIELD_NAME.match(k):
                raise ValueError(f"invalid field name {k!r}")
        used: dict[int, str] = {}
        for k, col in v.items():
            if col is None:
                continue
            if col in used:
                raise ValueError(f"column {col} is mapped to both {used[col]!r} and {k!r}")
            used[col] = k
        return v

    @classmethod
    def parse(cls, data: Any) -> MappingRequest:
        """Validate JSON data (dicts from the API, job payloads or the DB). Validation goes
        through JSON so the strict types mean JSON types: ``1`` is an index, ``"1"`` and
        ``true`` are rejected."""
        if isinstance(data, MappingRequest):
            return data
        return cls.model_validate_json(_json(data))

    @classmethod
    def from_stored(cls, data: Any) -> MappingRequest:
        """Read a mapping stored before this contract existed (``column_mappings`` rows).

        Old rows used the flat shape ``{field: value, <option>: value}`` written by the old
        UI: column choices were *integers* and the option inputs were *strings*. So an
        integer is always a column (also for ``counterparty``/``account``), and a string is
        only accepted for the option keys. Header-text column references cannot be resolved
        without the file and are rejected (the user confirms the mapping again)."""
        if isinstance(data, Mapping) and ("columns" in data or "options" in data):
            return cls.parse(data)
        if not isinstance(data, Mapping):
            raise ValueError("stored mapping must be an object")
        columns: dict[str, int | None] = {}
        options: dict[str, Any] = {}
        fmt = None
        for k, v in data.items():
            if k == "format_id":
                fmt = v
            elif k == "accept_suggested":
                options["accept_suggested"] = bool(v)
            elif k in ("counterparty", "account") and isinstance(v, str):
                options[f"{k}_override"] = v
            elif k in ("self_brn", "direction") and isinstance(v, str):
                options[k] = v
            elif v is None or (isinstance(v, int) and not isinstance(v, bool)):
                columns[k] = v
            else:
                raise ValueError(
                    f"stored mapping uses a header name for {k!r}; confirm the mapping again"
                )
        return cls.parse({"format_id": fmt, "columns": columns, "options": options})


class MappingContractError(ValueError):
    """A mapping that does not follow :class:`MappingRequest`."""


def _json(data: Any) -> str:
    import json

    try:
        return json.dumps(data, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise ValueError(f"mapping is not JSON data: {e}") from None


def mapping_error_text(e: ValidationError | ValueError) -> str:
    if isinstance(e, ValidationError):
        parts = []
        for err in e.errors()[:5]:
            loc = ".".join(str(x) for x in err.get("loc", ()))
            parts.append(f"{loc or '$'}: {err.get('msg', '')}")
        return "; ".join(parts)
    return str(e)


# --------------------------------------------------------------------------- parse result
class IssueKind(StrEnum):
    """``excluded``: the row produced no record. ``value``: the row was applied, but one of
    its cells could not be read and was left empty (e.g. an unreadable date)."""

    EXCLUDED = "excluded"
    VALUE = "value"


class RowIssueOut(_Strict):
    table: StrictStr
    row: StrictInt
    field: StrictStr | None = None
    message: StrictStr
    kind: Annotated[IssueKind, Field(strict=False)] = IssueKind.EXCLUDED


class TotalCheckOut(_Strict):
    """A 합계 row of the table compared with the sum of the table's data rows."""

    table: StrictStr
    row: StrictInt
    field: StrictStr
    stated: StrictInt
    computed: StrictInt
    matches: StrictBool

    @model_validator(mode="after")
    def _consistent(self) -> TotalCheckOut:
        if self.matches != (self.stated == self.computed):
            raise ValueError("matches must equal (stated == computed)")
        return self


class RowCounts(_Strict):
    """Row accounting of the tables a parser used (header, blank, 합계 and repeated header
    rows are not data rows).

    ``source_rows = applied_rows + excluded_rows``. ``tables_recognized = 0`` means no table
    of a known (or user-mapped) format was found - which is *not* the same as a recognised
    table that contains zero rows.

    ``tables_unread`` / ``unread_rows``: tables of the same file that had data rows but were
    not recognised as any known format (e.g. one sheet of a workbook with an unknown header
    row). Their rows are neither applied nor excluded - they were not read at all - so
    they are counted separately and make the application partial (``applied_needs_ack``)."""

    tables_recognized: Annotated[StrictInt, Field(ge=0)]
    source_rows: Annotated[StrictInt, Field(ge=0)]
    applied_rows: Annotated[StrictInt, Field(ge=0)]
    excluded_rows: Annotated[StrictInt, Field(ge=0)]
    tables_unread: Annotated[StrictInt, Field(ge=0)] = 0
    unread_rows: Annotated[StrictInt, Field(ge=0)] = 0

    @property
    def incomplete(self) -> bool:
        """Some source rows of this file were not turned into records."""
        return bool(self.excluded_rows or self.tables_unread)

    @model_validator(mode="after")
    def _sum(self) -> RowCounts:
        if self.applied_rows + self.excluded_rows != self.source_rows:
            raise ValueError("source_rows must equal applied_rows + excluded_rows")
        return self


LEDGER_TYPES = (Invoice, SettlementLine, BankTxn, Agreement)


class ParseResult(BaseModel):
    """Output of a parser for one document version (the API/worker ingest contract).

    ``status`` says whether the *format* was recognised (``PARSED``) - not whether every
    transaction was applied; ``counts``/``issues``/``totals`` say that. ``counts`` is
    required for ``PARSED`` results so a parser cannot report success without row
    accounting."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    status: DocumentStatus
    facts: tuple[Fact, ...] = ()
    records: tuple[Any, ...] = ()
    text: str | None = None
    reason: str | None = None
    suggestion: dict[str, Any] | None = None
    notes: tuple[str, ...] = ()
    issues: tuple[RowIssueOut, ...] = ()
    totals: tuple[TotalCheckOut, ...] = ()
    counts: RowCounts | None = None

    @model_validator(mode="after")
    def _invariants(self) -> ParseResult:
        if self.status is DocumentStatus.PARSED and self.counts is None:
            raise ValueError("a PARSED result must report row counts")
        if self.status is not DocumentStatus.PARSED and (self.records or self.facts):
            raise ValueError(f"a {self.status.value} result must not carry records or facts")
        if self.counts is not None and self.counts.applied_rows != len(self.records):
            raise ValueError("counts.applied_rows must equal the number of records")
        for f in self.facts:
            if not isinstance(f, Fact):
                raise ValueError(f"facts must be Fact objects, got {type(f).__name__}")
        for r in self.records:
            if not isinstance(r, LEDGER_TYPES):
                raise ValueError(f"unsupported record type {type(r).__name__}")
            # Boundary check: a counterparty must be text. A column index or any other
            # value that slipped through a mapping must never become a company name.
            cp = getattr(r, "counterparty", None)
            if cp is not None and not isinstance(cp, str):
                raise ValueError(f"record {r.id}: counterparty must be text, got {cp!r}")
        return self

    @property
    def ok(self) -> bool:
        return self.status is DocumentStatus.PARSED

    @property
    def totals_mismatched(self) -> tuple[TotalCheckOut, ...]:
        return tuple(t for t in self.totals if not t.matches)


# --------------------------------------------------------------------------- application
class ApplyState(StrEnum):
    """What applying one parsed document version did to the current ledger."""

    APPLIED = "applied"  # now current; every data row applied, totals agree
    APPLIED_NEEDS_ACK = (
        "applied_needs_ack"  # now current; rows excluded/values unread/totals differ
    )
    APPLIED_EMPTY = "applied_empty"  # now current; a verified empty table replaced old rows
    UNCHANGED = "unchanged"  # already current with an identical parse result
    NOT_PROMOTED_OLDER = "not_promoted_older"  # a newer version is current; kept for history
    NOT_APPLIED_PARSE = "not_applied_parse"  # format not read (mapping/corrupt/scan): kept data
    NOT_APPLIED_NO_TABLE = "not_applied_no_table"  # no table found: existing data kept
    NOT_APPLIED_ALL_EXCLUDED = "not_applied_all_excluded"  # every row excluded: existing kept
    NOT_APPLIED_EMPTY_UNVERIFIED = "not_applied_empty_unverified"  # 0 rows but 합계 != 0: kept


CURRENT_STATES = frozenset(
    {ApplyState.APPLIED, ApplyState.APPLIED_NEEDS_ACK, ApplyState.APPLIED_EMPTY}
)


class ApplyOutcome(_Strict):
    state: Annotated[ApplyState, Field(strict=False)]
    message: StrictStr
    document_id: StrictStr
    doc_version_id: StrictStr
    version: StrictInt
    current_doc_version_id: StrictStr | None
    current_version: StrictInt | None
    previous_version: StrictInt | None
    records_added: StrictInt = 0
    records_updated: StrictInt = 0
    records_removed: StrictInt = 0
    # Records of earlier versions that have no counterpart in this partially read version
    # (rows excluded or tables unread): kept in the ledger, still citing the version they
    # came from, until the user acknowledges this version (which removes them). Never
    # counted in ``records_removed``.
    records_carried_over: StrictInt = 0
    carried_over: tuple[StrictStr, ...] = ()  # "<entity>:<record id>", at most 200
    ack_required: StrictBool = False
    acknowledged: StrictBool = False
    fingerprint: StrictStr


class MappingSource(_Strict):
    """Which column mapping an ingest job used.

    ``job``: the mapping sent with the job; ``confirmed``: the mapping a user confirmed for
    this version; ``inherited``: the mapping a user confirmed for an earlier version of the
    same document, reused because that version's header rows are identical; ``none``:
    automatic recognition only. ``not_inherited`` explains why an earlier confirmed mapping
    was not reused (the headers changed), in which case the version needs a mapping."""

    kind: Literal["job", "confirmed", "inherited", "none"]
    doc_version_id: StrictStr | None = None
    version: StrictInt | None = None
    mapping_id: StrictStr | None = None
    not_inherited: StrictStr | None = None


class IngestJobResult(_Strict):
    """``ingest_document`` job result (stored as JSON in ``jobs.result``)."""

    doc_version_id: StrictStr
    document_id: StrictStr
    version: StrictInt
    document_status: Annotated[DocumentStatus, Field(strict=False)]
    reason: StrictStr | None
    facts: StrictInt
    records: StrictInt
    counts: RowCounts | None
    issues: tuple[RowIssueOut, ...]
    issues_total: StrictInt
    totals: tuple[TotalCheckOut, ...]
    notes: tuple[StrictStr, ...]
    application: ApplyOutcome
    impact: dict[str, Any] | None = None
    mapping: MappingSource | None = None


MAX_ISSUES_STORED = 200
