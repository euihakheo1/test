"""Ingestion pipeline: parse → recognise/map → (confirmed mapping) → records + facts.

``analyze`` decides the document status without creating ids; ``build_records`` turns the
usable tables into ledger records and facts for a concrete document version;
``parse_document`` is the API/worker entrypoint and returns a strict
:class:`jettae.app.contracts.ParseResult`; ``ingest_document`` registers and applies a file
in-process through :class:`jettae.app.doc_apply.DocumentApplier` (the same application
rules as the worker).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from jettae.app.contracts import (
    ApplyOutcome,
    IssueKind,
    MappingContractError,
    MappingRequest,
    ParseResult,
    RowCounts,
    RowIssueOut,
    TotalCheckOut,
    mapping_error_text,
)
from jettae.domain.dates import utc_now
from jettae.domain.hashing import to_canonical
from jettae.domain.models import Change, DocumentVersion, Fact
from jettae.domain.status import DocKind, DocumentStatus
from jettae.ingest.cells import ParsedDoc, Table
from jettae.ingest.detect import MEDIA_TYPES, safe_parse
from jettae.ingest.formats import FORMATS, MIN_SCORE, Recognition, recognize_table
from jettae.ingest.formats.base import (
    BuildContext,
    FormatSpec,
    IngestOptions,
    RowIssue,
    TableExtract,
    TotalCheck,
    extract_table,
)
from jettae.ingest.mapping import MappingSuggester, MappingSuggestion, confirm_mapping
from jettae.ingest.ocrhook import OcrProvider
from jettae.ingest.security import DEFAULT_LIMITS, Limits, safe_filename


@dataclass
class TablePlan:
    table: str
    recognition: Recognition
    spec: FormatSpec | None
    mapping: MappingSuggestion | None
    usable: bool  # records will be built from this table
    relevant: bool  # recognised (or user-mapped) as a known format
    reason: str = ""
    data_rows: int = 0  # non-header, non-합계 rows of the table
    user_mapped: bool = False  # a user mapping (or a forced format) was applied to it

    @property
    def unread(self) -> bool:
        """A table with data rows that was not recognised as any known format. Its rows are
        reported as *unread* - never treated as "this sheet was deleted"."""
        return not self.relevant and self.data_rows > 0


@dataclass
class IngestPlan:
    parsed: ParsedDoc
    tables: list[TablePlan]
    status: DocumentStatus
    doc_kind: DocKind
    notes: list[str] = field(default_factory=list)


@dataclass
class IngestResult:
    plan: IngestPlan
    records: list[Any] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    issues: list[RowIssue] = field(default_factory=list)
    totals: list[TotalCheck] = field(default_factory=list)
    extracts: list[TableExtract] = field(default_factory=list)
    application: ApplyOutcome | None = None  # set by ingest_document

    @property
    def status(self) -> DocumentStatus:
        return self.plan.status

    @property
    def counts(self) -> RowCounts:
        """Row accounting over the tables records were built from, plus the tables that had
        data rows but were not recognised (``tables_unread`` / ``unread_rows``), so a result
        can never claim "every row applied" while a sheet was skipped."""
        source = sum(x.source_rows for x in self.extracts)
        excluded = sum(x.skipped_rows for x in self.extracts)
        unread = [tp for tp in self.plan.tables if tp.unread]
        return RowCounts(
            tables_recognized=len(self.extracts),
            source_rows=source,
            applied_rows=source - excluded,
            excluded_rows=excluded,
            tables_unread=len(unread),
            unread_rows=sum(tp.data_rows for tp in unread),
        )


def _auto_confirmed(rec: Recognition) -> bool:
    """Recognised automatically with confirmed confidence: complete, above the score
    threshold, and every required column matched with high/confirmed confidence."""
    best = rec.best
    return bool(
        best is not None
        and best.complete
        and best.score >= MIN_SCORE
        and not best.mapping.needs_confirmation
    )


def _data_rows(t: Table, rec: Recognition) -> int:
    if rec.view.headers:
        return len(rec.view.data)
    return sum(1 for r in t.rows if not r.is_empty)


def _mapping_targets(
    opts: IngestOptions, tables: list[Table], auto: dict[str, Recognition]
) -> dict[str, Mapping[str, int | str | None]]:
    """The tables a user mapping (``opts.mapping`` and/or a forced ``opts.format_id``)
    applies to.

    A mapping describes ONE table layout: column 1 of sheet A is not column 1 of sheet B.
    Applying it to a table with another column order silently swaps amounts and
    references, so:

    * ``{table_name: fields}`` (``MappingRequest.table``): that table, plus each other table
      that was *not* recognised with confirmed confidence and has exactly the same header
      row (e.g. the pages of one PDF statement). An unknown name is a contract error.
    * ``{"*": fields}`` (no table named) or a forced format alone: with one table, that
      table. With several tables, the tables not recognised with confirmed confidence,
      provided they share one header row; otherwise the request is ambiguous and is
      rejected (the caller must name the table). A table recognised with confirmed
      confidence is never overridden by an unnamed mapping.
    """
    named = [k for k in opts.mapping if k != "*"]
    if not named and "*" not in opts.mapping and not opts.format_id:
        return {}
    by_name = {t.name: t for t in tables}
    out: dict[str, Mapping[str, int | str | None]] = {}
    if named:
        for name in named:
            if name not in by_name:
                known = ", ".join(sorted(by_name)) or "-"
                raise MappingContractError(
                    f"the mapping names table {name!r}, which has no data in this file "
                    f"(tables: {known})"
                )
            fields = opts.mapping[name]
            out[name] = fields
            headers = auto[name].view.headers
            for t in tables:
                if t.name in out or _auto_confirmed(auto[t.name]):
                    continue
                if headers and auto[t.name].view.headers == headers:
                    out[t.name] = fields
        return out
    fields = opts.mapping.get("*", {})
    if len(tables) <= 1:
        return {t.name: fields for t in tables}
    open_tables = [t for t in tables if not _auto_confirmed(auto[t.name])]
    layouts = {auto[t.name].view.headers for t in open_tables}
    if not open_tables or len(layouts) > 1:
        names = ", ".join(t.name for t in tables)
        raise MappingContractError(
            f"this file has {len(tables)} tables ({names}); the mapping must name the table "
            "it was made for (table)"
        )
    return {t.name: fields for t in open_tables}


def analyze(
    parsed: ParsedDoc,
    options: IngestOptions | None = None,
    *,
    suggester: MappingSuggester | None = None,
) -> IngestPlan:
    """Recognise every table and decide the document status (no ids are created).

    * A relevant table (recognised, or user-mapped) that cannot be used makes the document
      ``NEEDS_MAPPING``; so does a file in which no table is recognised.
    * A file with usable tables and *some* unrecognised tables with data rows is
      ``PARSED``, but those tables are reported as unread (``TablePlan.unread`` ->
      ``RowCounts.tables_unread``). The application layer then treats the version as
      partially read: it is never "every row applied", and the rows such a table produced
      in an earlier version are not removed (see :mod:`jettae.app.doc_apply`).

    Raises :class:`MappingContractError` when a user mapping cannot be attributed to one
    table layout (see :func:`_mapping_targets`)."""
    opts = options or IngestOptions()
    if not parsed.ok:
        return IngestPlan(parsed, [], parsed.status, DocKind.OTHER, [parsed.reason or ""])
    tables = [t for t in parsed.tables if not all(r.is_empty for r in t.rows)]
    auto = {t.name: recognize_table(t, suggester=suggester) for t in tables}
    targets = _mapping_targets(opts, tables, auto)
    plans: list[TablePlan] = []
    for t in tables:
        targeted = t.name in targets
        user = targets.get(t.name) or None
        rec = (
            recognize_table(t, format_id=opts.format_id, suggester=suggester)
            if targeted and opts.format_id
            else auto[t.name]
        )
        rows = _data_rows(t, rec)

        def plan(
            spec: FormatSpec | None,
            mapping: MappingSuggestion | None,
            usable: bool,
            relevant: bool,
            reason: str = "",
            *,
            _t: Table = t,
            _rec: Recognition = rec,
            _rows: int = rows,
            _targeted: bool = targeted,
        ) -> TablePlan:
            return TablePlan(
                _t.name, _rec, spec, mapping, usable, relevant, reason, _rows, _targeted
            )

        best = rec.best
        if best is None:
            plans.append(plan(None, None, False, False, "no header found"))
            continue
        spec, mapping = best.spec, best.mapping
        if user:
            try:
                mapping = confirm_mapping(mapping, best.headers, user, spec.fields)
            except ValueError as e:
                plans.append(plan(spec, mapping, False, True, f"mapping rejected: {e}"))
                continue
        complete = spec.complete(mapping)
        relevant = targeted or (complete and best.score >= MIN_SCORE)
        if not relevant:
            plans.append(plan(spec, mapping, False, False, "format not recognised"))
            continue
        if not complete:
            plans.append(plan(spec, mapping, False, True, "required columns not mapped"))
            continue
        if mapping.needs_confirmation and not opts.accept_suggested:
            plans.append(plan(spec, mapping, False, True, "mapping needs confirmation"))
            continue
        plans.append(plan(spec, mapping, True, True))
    notes: list[str] = []
    relevant_plans = [p for p in plans if p.relevant]
    if any(p.relevant and not p.usable for p in plans):
        status = DocumentStatus.NEEDS_MAPPING
    elif not relevant_plans and plans:
        status = DocumentStatus.NEEDS_MAPPING
        notes.append("no table matched a known format: confirm the column mapping")
    else:
        status = DocumentStatus.PARSED
        if not plans:
            notes.append("no tabular data found")
        unread = [p for p in plans if p.unread]
        if unread:
            notes.append(
                "tables not read (format not recognised; their rows were not applied): "
                + ", ".join(f"{p.table} ({p.data_rows} rows)" for p in unread)
            )
    kinds = {p.spec.doc_kind for p in relevant_plans if p.spec is not None}
    doc_kind = kinds.pop() if len(kinds) == 1 else DocKind.OTHER
    return IngestPlan(parsed, plans, status, doc_kind, notes)


def build_records(
    plan: IngestPlan,
    *,
    tenant_id: str,
    doc_version_id: str,
    document_key: str | None = None,
    observed_at: datetime | None = None,
    options: IngestOptions | None = None,
) -> IngestResult:
    opts = options or IngestOptions()
    res = IngestResult(plan)
    for tp in plan.tables:
        if not tp.usable or tp.spec is None or tp.mapping is None:
            continue
        view = tp.recognition.view
        hint = "\n".join([view.preamble, tp.table, plan.parsed.filename])
        ctx = BuildContext(
            tenant_id=tenant_id,
            doc_version_id=doc_version_id,
            document_key=document_key or doc_version_id,
            observed_at=observed_at or utc_now(),
            options=opts,
            epoch_1904=bool(plan.parsed.meta.get("epoch_1904")),
            doc_hint=hint,
        )
        ex = extract_table(ctx, tp.spec, view, tp.mapping)
        res.extracts.append(ex)
        res.records.extend(ex.records)
        res.facts.extend(ex.facts)
        res.issues.extend(ex.issues)
        res.totals.extend(ex.totals)
    return res


def ingest_bytes(
    content: bytes,
    filename: str = "",
    *,
    tenant_id: str = "local",
    doc_version_id: str = "local",
    options: IngestOptions | None = None,
    limits: Limits = DEFAULT_LIMITS,
    ocr: OcrProvider | None = None,
    suggester: MappingSuggester | None = None,
    observed_at: datetime | None = None,
) -> IngestResult:
    """Parse + analyse + build without persisting (CLI, previews)."""
    parsed = safe_parse(content, safe_filename(filename), limits=limits, ocr=ocr)
    plan = analyze(parsed, options, suggester=suggester)
    return build_records(
        plan,
        tenant_id=tenant_id,
        doc_version_id=doc_version_id,
        observed_at=observed_at,
        options=options,
    )


def ingest_document(
    service: Any,
    tenant_id: str,
    *,
    filename: str,
    content: bytes,
    document_id: str | None = None,
    options: IngestOptions | None = None,
    limits: Limits = DEFAULT_LIMITS,
    ocr: OcrProvider | None = None,
    suggester: MappingSuggester | None = None,
) -> tuple[DocumentVersion, IngestResult, list[Change]]:
    """Register the upload as a document version and apply it (in-process path: CLI, demo,
    tests). The ledger change goes through :class:`jettae.app.doc_apply.DocumentApplier`,
    exactly like the job worker, so version promotion, the zero-row rule, the parse-failure
    rule and the partial-application policy are the same on both paths.

    Returns the registered version, the raw extraction (``result.application`` holds the
    :class:`~jettae.app.contracts.ApplyOutcome`) and the ledger/fact changes applied."""
    from jettae.app.doc_apply import DocumentApplier

    name = safe_filename(filename)
    parsed = safe_parse(content, name, limits=limits, ocr=ocr)
    plan = analyze(parsed, options, suggester=suggester)
    media = MEDIA_TYPES.get(parsed.source_kind, "application/octet-stream")
    doc: DocumentVersion = service.register_document(
        tenant_id,
        filename=name,
        content=content,
        media_type=media,
        kind=plan.doc_kind,
        document_id=document_id,
        text=parsed.text or None,
        status=plan.status,
    )
    result = build_records(
        plan,
        tenant_id=tenant_id,
        doc_version_id=doc.id,
        document_key=doc.document_id,
        observed_at=doc.created_at,
        options=options,
    )
    outcome = to_parse_result(plan, result, media_type="")
    report = DocumentApplier(service).apply(tenant_id, doc, outcome)
    result.application = report.outcome
    return doc, result, list(report.changes)


def plan_summary(plan: IngestPlan) -> dict[str, Any]:
    p = plan.parsed
    tables = []
    for tp in plan.tables:
        v = tp.recognition.view
        tables.append(
            {
                "table": tp.table,
                "header_rows": list(v.header_rows),
                "headers": list(v.headers),
                "data_rows": len(v.data),
                "total_rows": [r.index for r in v.totals],
                "skipped_rows": [list(s) for s in v.skipped],
                "format": tp.spec.id if tp.spec else None,
                "usable": tp.usable,
                "reason": tp.reason,
                "candidates": [
                    {"format": c.spec.id, "score": c.score, "complete": c.complete}
                    for c in tp.recognition.candidates
                ],
                "mapping": tp.mapping.as_dict() if tp.mapping else None,
                "notes": list(v.notes),
            }
        )
    return {
        "file": p.filename,
        "source_kind": p.source_kind.value,
        "status": plan.status.value,
        "doc_kind": plan.doc_kind.value,
        "encoding": p.encoding,
        "delimiter": p.delimiter,
        "reason": p.reason,
        "warnings": list(p.warnings),
        "notes": list(plan.notes),
        "meta": to_canonical(p.meta),
        "tables": tables,
    }


# ---------------------------------------------------------------------- API / worker bridge
# Contract used by ``jettae.db.ingest_bridge`` (entrypoint ``jettae.ingest.pipeline``).
_KIND_FORMAT = {
    "bank": "kr_bank_txn",
    "tax_invoice": "hometax_etax_list",
    "settlement": "retail_settlement",
    "agreement": "agreement_terms",
}

# Kept as a name for older imports: the bridge result *is* the strict contract model.
ParseOutcome = ParseResult


def options_from_mapping(
    mapping: MappingRequest | Mapping[str, Any] | None, kind: str = ""
) -> IngestOptions:
    """Turn a :class:`MappingRequest` into :class:`IngestOptions`.

    ``columns`` are column *indexes* (``None`` = the user removed that field's column);
    ``options`` are document-level *values*. Without ``format_id`` the format is the only
    one whose fields contain every mapped column (or the uploaded document kind's format,
    when that one fits)."""
    if mapping is None:
        return IngestOptions()
    try:
        req = MappingRequest.parse(mapping)
    except (ValidationError, ValueError) as e:
        raise MappingContractError(mapping_error_text(e)) from None
    fields: dict[str, int | str | None] = dict(req.columns)
    fmt: str | None = req.format_id
    if fmt is None and fields:
        fits = [f.id for f in FORMATS if set(fields) <= {fs.name for fs in f.fields}]
        if len(fits) == 1:
            fmt = fits[0]
        elif _KIND_FORMAT.get(kind) in fits:
            fmt = _KIND_FORMAT[kind]
    o = req.options
    return IngestOptions(
        counterparty_override=o.counterparty_override,
        self_brn=o.self_brn,
        direction=o.direction,
        account_override=o.account_override,
        accept_suggested=o.accept_suggested,
        format_id=fmt,
        # A named table gets the mapping (and the forced format) alone; "*" = the caller did
        # not name a table, see ``_mapping_targets`` for when that is accepted.
        mapping={req.table: fields} if req.table else ({"*": fields} if fields else {}),
    )


def _first_suggestion(plan: IngestPlan) -> dict[str, Any] | None:
    ordered = sorted(plan.tables, key=lambda tp: (tp.usable, not tp.relevant))
    for tp in ordered:
        if tp.mapping is not None and tp.spec is not None:
            d = tp.mapping.as_dict()
            d["table"] = tp.table
            d["headers"] = list(tp.recognition.view.headers)
            d["fields"] = [
                {"name": f.name, "label": f.label, "kind": f.kind.value, "required": f.required}
                for f in tp.spec.fields
            ]
            return d
    return None


def to_parse_result(plan: IngestPlan, res: IngestResult, *, media_type: str = "") -> ParseResult:
    """Strict contract view of an extraction (status, records, facts, issues, totals, counts)."""
    parsed = plan.parsed
    notes = [*parsed.warnings, *plan.notes]
    notes += [f"{tp.table}: {tp.reason}" for tp in plan.tables if tp.reason]
    if media_type and MEDIA_TYPES.get(parsed.source_kind) not in (media_type, None):
        notes.append(
            f"declared media type {media_type} differs from content ({parsed.source_kind})"
        )
    ok = plan.status is DocumentStatus.PARSED
    return ParseResult(
        status=plan.status,
        facts=tuple(res.facts) if ok else (),
        records=tuple(res.records) if ok else (),
        text=parsed.text or None,
        reason=parsed.reason
        or (
            "column mapping must be confirmed"
            if plan.status is DocumentStatus.NEEDS_MAPPING
            else None
        ),
        suggestion=_first_suggestion(plan),
        notes=tuple(notes),
        issues=tuple(
            RowIssueOut(
                table=i.table, row=i.row, field=i.field, message=i.message, kind=IssueKind(i.kind)
            )
            for i in res.issues
        ),
        totals=tuple(
            TotalCheckOut(
                table=t.table,
                row=t.row,
                field=t.field,
                stated=t.stated,
                computed=t.computed,
                matches=t.matches,
            )
            for t in res.totals
        ),
        counts=res.counts if ok else None,
    )


def parse_document(
    content: bytes,
    *,
    filename: str,
    media_type: str = "",
    kind: str = "other",
    tenant_id: str,
    doc_version_id: str,
    mapping: MappingRequest | Mapping[str, Any] | None = None,
    document_key: str | None = None,
    observed_at: datetime | None = None,
    limits: Limits = DEFAULT_LIMITS,
    ocr: OcrProvider | None = None,
    suggester: MappingSuggester | None = None,
) -> ParseResult:
    """Parse + map + build for an already registered document version (API worker).

    ``mapping`` must follow :class:`MappingRequest` (``MappingContractError`` otherwise).
    ``media_type`` is informational: the file type is sniffed from the content.
    ``observed_at`` (the version's registration time) makes repeated parses of the same
    version produce identical facts, so re-applying them is recognised as "unchanged"."""
    opts = options_from_mapping(mapping, kind)
    parsed = safe_parse(content, safe_filename(filename), limits=limits, ocr=ocr)
    plan = analyze(parsed, opts, suggester=suggester)
    res = build_records(
        plan,
        tenant_id=tenant_id,
        doc_version_id=doc_version_id,
        document_key=document_key,
        observed_at=observed_at,
        options=opts,
    )
    return to_parse_result(plan, res, media_type=media_type)


def suggest_mapping(
    content: bytes,
    *,
    filename: str,
    media_type: str = "",
    kind: str = "other",
    limits: Limits = DEFAULT_LIMITS,
    suggester: MappingSuggester | None = None,
) -> dict[str, Any]:
    """JSON-compatible mapping suggestion for a stored file (API ``GET .../mapping``)."""
    parsed = safe_parse(content, safe_filename(filename), limits=limits)
    plan = analyze(parsed, IngestOptions(), suggester=suggester)
    out = plan_summary(plan)
    out["suggestion"] = _first_suggestion(plan)
    out["declared_kind"] = kind
    out["declared_media_type"] = media_type
    return out
