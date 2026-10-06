"""Request / response models (Pydantic v2). Money is ``{"amount": int, "currency": str}``;
dates are ISO strings. No float fields anywhere."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from jettae.app.contracts import MappingRequest

JobType = Literal["ingest_document", "run_analysis", "apply_change"]
Role = Literal["viewer", "member", "admin", "owner"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------ auth
class SignupRequest(_M):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)
    tenant_name: str = Field(max_length=200, description="회사(테넌트) 이름")


class LoginRequest(_M):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)
    tenant_id: str | None = Field(
        None, max_length=128, description="Only needed when the account has several tenants"
    )


class SessionUser(BaseModel):
    id: str
    email: str
    role: str


class SessionTenant(BaseModel):
    id: str
    name: str | None


class SessionResponse(BaseModel):
    """Body of login / signup / refresh. The tokens themselves are only in HttpOnly cookies;
    this body deliberately has no token fields."""

    user: SessionUser
    tenant: SessionTenant
    tenant_id: str
    role: str
    expires_in: int = Field(description="access cookie lifetime in seconds")
    session_expires_at: datetime | None = Field(
        None, description="absolute end of the session; refresh cannot extend it"
    )


class MeResponse(BaseModel):
    user: SessionUser
    tenant: SessionTenant
    # flat fields kept for clients written before {user, tenant}
    user_id: str
    email: str
    tenant_id: str
    tenant_name: str | None
    role: str
    auth_method: str


class ApiTokenCreate(_M):
    name: str = Field(max_length=200)
    role: Role = "member"
    expires_in_days: int | None = Field(None, ge=1, le=3650)


class ApiTokenOut(BaseModel):
    id: str
    name: str
    role: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class ApiTokenCreated(ApiTokenOut):
    token: str = Field(description="Shown once; only a hash is stored")


# ------------------------------------------------------------------------------ documents
class DocumentVersionOut(BaseModel):
    id: str
    document_id: str
    version: int
    filename: str
    media_type: str
    kind: str
    size: int
    content_hash: str
    status: str
    supersedes: str | None
    created_at: datetime
    status_detail: dict[str, Any] | None = None
    apply_state: str | None = Field(
        None,
        description=(
            "What applying this version did (applied, applied_needs_ack, applied_empty, "
            "unchanged, not_promoted_older, not_applied_*); null = not applied yet. "
            "`status` only says whether the format was read."
        ),
    )
    issue_count: int | None = None
    is_current: bool = Field(False, description="this version's rows are the ones in the ledger")
    current_version: int | None = Field(None, description="version number currently applied")


class UploadResponse(BaseModel):
    document: DocumentVersionOut
    duplicate: bool
    job_id: str | None


class DocumentOut(BaseModel):
    document_id: str
    latest: DocumentVersionOut


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    next_cursor: str | None


class DocumentHeadOut(BaseModel):
    document_id: str
    doc_version_id: str
    version: int
    state: str
    fingerprint: str
    needs_ack: bool
    acknowledged: bool
    applied_at: datetime
    # acknowledgment response only: records carried over from earlier versions that the
    # acknowledgment removed ("<entity>:<id>", at most 200 listed)
    records_removed: int | None = None
    removed: list[str] | None = None
    ack_by: str | None
    ack_at: datetime | None


class VersionList(BaseModel):
    document_id: str
    items: list[DocumentVersionOut]
    current: DocumentHeadOut | None = None


class AcknowledgeRequest(_M):
    fingerprint: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="application.fingerprint of the parse result that was reviewed",
    )


class SpanOut(BaseModel):
    fact_id: str
    kind: str
    subject_id: str | None
    value: Any
    locator: dict[str, Any]
    excerpt: str


class SpanPage(BaseModel):
    items: list[SpanOut]
    next_cursor: str | None


class MappingOut(BaseModel):
    doc_version_id: str
    document_status: str
    suggestion: dict[str, Any] | None
    confirmed: dict[str, Any] | None


class MappingConfirm(_M):
    mapping: MappingRequest = Field(
        description=(
            '{"format_id"?, "columns": {field: column index | null}, "options": '
            '{"counterparty_override"?, "account_override"?, "self_brn"?, "direction"?}}'
        )
    )
    reingest: bool = True


class MappingConfirmed(BaseModel):
    mapping_id: str
    doc_version_id: str
    job_id: str | None


# ------------------------------------------------------------------------------ jobs
class JobCreate(_M):
    type: JobType
    params: dict[str, Any] = Field(default_factory=dict)


class JobOut(BaseModel):
    id: str
    type: str
    status: str
    attempts: int
    max_attempts: int
    payload: dict[str, Any]
    result: Any
    error: Any
    cancel_requested: bool
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    run_after: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobAccepted(BaseModel):
    job_id: str
    status: str
    status_url: str


class JobPage(BaseModel):
    items: list[JobOut]
    next_cursor: str | None


# ------------------------------------------------------------------------------ decisions
class DecisionSummary(BaseModel):
    id: str
    subject_id: str
    status: str
    review_status: str
    result_hash: str
    snapshot_hash: str
    required_documents: list[str]
    unresolved: list[str]
    missing: list[str]
    # Document evidence of the receivable (computation "evidence"): which document the
    # amount comes from, and whether it counts in receivable totals. ``counted`` is false
    # for a document awaiting the user's "same sale?" confirmation (shown as 확인 대기;
    # its amount must not be added to totals). None for decisions without that computation.
    basis: str | None = None
    basis_id: str | None = None
    counted: bool | None = None


class DecisionPage(BaseModel):
    items: list[DecisionSummary]
    next_cursor: str | None


class FactOut(BaseModel):
    id: str
    kind: str
    subject_id: str | None
    value: Any
    extractor: str
    observed_at: datetime
    span: dict[str, Any] | None


class CheckOut(BaseModel):
    name: str
    passed: bool
    details: list[str]


class ApprovalOut(BaseModel):
    id: str
    decision_id: str
    result_hash: str
    snapshot_hash: str
    approved_by: str
    approved_at: datetime
    current: bool


class HistoryOut(BaseModel):
    recorded_at: datetime
    result_hash: str
    status: str
    superseded: bool


class DecisionDetail(DecisionSummary):
    facts: list[FactOut]
    computations: list[dict[str, Any]]
    variants: list[dict[str, Any]]
    allocations: list[dict[str, Any]]
    assumptions: list[str]
    rule_versions: list[str]
    explanation: str
    checks: list[CheckOut]
    approvals: list[ApprovalOut]
    history: list[HistoryOut]


class ApprovalCreate(_M):
    expected_result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvidenceLinkCreate(_M):
    """Answer to "is this tax invoice the same sale as a settlement line?"."""

    expected_result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation: Literal["same_sale", "separate_sale"]
    settlement_line_id: str | None = Field(None, min_length=1, max_length=200)
    invoice_id: str | None = Field(
        None,
        min_length=1,
        max_length=200,
        description="defaults to the decision's basis invoice",
    )
    note: str = Field("", max_length=1000)


class EvidenceLinkOut(BaseModel):
    id: str
    # the confirmed document's id: a tax invoice, or (document_entity = settlement_line) a
    # settlement line flagged as a possible duplicate of another document's line
    invoice_id: str
    relation: str
    settlement_line_id: str | None
    confirmed_by: str
    note: str
    document_entity: str = "invoice"


class EvidenceLinkPage(BaseModel):
    items: list[EvidenceLinkOut]


class LinkOutcome(BaseModel):
    link: EvidenceLinkOut | None
    changed: list[str]
    review_required: list[str]
    removed: list[str]
    snapshot_hash: str


# ------------------------------------------------------------------------------ changes
class ChangesRequest(_M):
    changes: list[dict[str, Any]] = Field(
        min_length=1,
        max_length=5000,
        description=(
            'items: {"kind": "add|update|remove", "entity": "invoice|settlement_line|'
            'bank_txn|agreement|fact", "id": "...", "record": {...}}. tenant_id is '
            "taken from the credential; a client-sent tenant_id is ignored."
        ),
    )


# ------------------------------------------------------------------------------ reports
class ReportRequest(_M):
    decision_ids: list[str] | None = Field(None, max_length=5000)
    format: Literal["csv", "html"] = "csv"
    require_approved: bool = False


class HealthOut(BaseModel):
    status: str
    checks: dict[str, Any] = Field(default_factory=dict)
