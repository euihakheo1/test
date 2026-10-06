"""/decisions: list with filters, detail, approvals."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from jettae.api.deps import (
    IdemKey,
    Member,
    RuntimeDep,
    Viewer,
    clamp_limit,
    fingerprint,
    get_ops,
    idempotent,
)
from jettae.api.errors import ERROR_RESPONSES
from jettae.api.schemas import (
    ApprovalCreate,
    ApprovalOut,
    DecisionDetail,
    DecisionPage,
    EvidenceLinkCreate,
    EvidenceLinkPage,
    LinkOutcome,
)

router = APIRouter(prefix="/decisions", tags=["decisions"], responses=ERROR_RESPONSES)
links_router = APIRouter(prefix="/evidence-links", tags=["decisions"], responses=ERROR_RESPONSES)
OpsDep = Annotated[Any, Depends(get_ops)]


@router.get("", response_model=DecisionPage)
def list_decisions(
    ops: OpsDep,
    p: Viewer,
    status: Annotated[list[str] | None, Query(description="ReconcileStatus (repeatable)")] = None,
    review_status: Annotated[
        list[str] | None, Query(description="ReviewStatus (repeatable)")
    ] = None,
    subject_id: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> Any:
    return ops.list_decisions(
        p,
        after=cursor,
        limit=clamp_limit(limit),
        statuses=status or [],
        review_statuses=review_status or [],
        subject_id=subject_id,
    )


@router.get("/{decision_id}", response_model=DecisionDetail)
def decision_detail(decision_id: str, ops: OpsDep, p: Viewer) -> Any:
    """사실·원문 위치·계산·필요 서류·계산 방식별 결과(variants)·승인 이력."""
    return ops.decision_detail(p, decision_id)


@router.get("/{decision_id}/approvals")
def list_approvals(decision_id: str, ops: OpsDep, p: Viewer) -> Any:
    return ops.approvals(p, decision_id)


@router.post(
    "/{decision_id}/approvals",
    status_code=201,
    response_model=ApprovalOut,
    responses={409: {"description": "expected_result_hash is not the current result"}},
)
def approve(
    decision_id: str,
    body: ApprovalCreate,
    rt: RuntimeDep,
    ops: OpsDep,
    p: Member,
    idem: IdemKey = None,
) -> JSONResponse:
    """현재 결과를 확인(승인)한다. 기대 결과 해시가 현재와 다르면 409."""
    return idempotent(
        rt,
        p,
        idem,
        f"POST /decisions/{decision_id}/approvals",
        fingerprint(body.model_dump()),
        lambda: ops.approve(p, decision_id, body.expected_result_hash),
    )


@router.post(
    "/{decision_id}/evidence-link",
    response_model=LinkOutcome,
    responses={409: {"description": "expected_result_hash is not the current result"}},
)
def confirm_evidence_link(
    decision_id: str,
    body: EvidenceLinkCreate,
    rt: RuntimeDep,
    ops: OpsDep,
    p: Member,
    idem: IdemKey = None,
) -> JSONResponse:
    """세금계산서가 정산 행과 같은 거래인지(same_sale), 별개 거래인지(separate_sale) 확인을
    기록하고 영향받는 결과만 다시 계산한다. 금액·날짜가 같다는 이유만으로는 연결하지 않으므로
    확인 대기(AMBIGUOUS, unresolved=evidence_link) 문서는 이 확인이 있어야 합계에 들어간다."""
    return idempotent(
        rt,
        p,
        idem,
        f"POST /decisions/{decision_id}/evidence-link",
        fingerprint(body.model_dump()),
        lambda: ops.confirm_link(p, decision_id, body.model_dump()),
    )


@links_router.get("", response_model=EvidenceLinkPage)
def list_evidence_links(ops: OpsDep, p: Viewer) -> Any:
    return ops.list_links(p)


@links_router.delete("/{link_id}", response_model=LinkOutcome)
def withdraw_evidence_link(link_id: str, ops: OpsDep, p: Member) -> Any:
    """사용자 확인을 취소한다. 세금계산서는 자동 연결 규칙(참조번호 일치, 아니면 확인 대기)으로
    돌아간다."""
    return ops.withdraw_link(p, link_id)
