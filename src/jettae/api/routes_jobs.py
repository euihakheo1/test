"""/jobs: create (202), status, list, cancel."""

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
    decode_cursor,
    encode_cursor,
    fingerprint,
    get_ops,
    idempotent,
)
from jettae.api.errors import ERROR_RESPONSES
from jettae.api.schemas import JobAccepted, JobCreate, JobOut, JobPage

router = APIRouter(prefix="/jobs", tags=["jobs"], responses=ERROR_RESPONSES)
OpsDep = Annotated[Any, Depends(get_ops)]


def location(body: Any) -> dict[str, str]:
    url = body.get("status_url") if isinstance(body, dict) else None
    return {"Location": url} if url else {}


@router.post("", status_code=202, response_model=JobAccepted)
def create_job(
    body: JobCreate, rt: RuntimeDep, ops: OpsDep, p: Member, idem: IdemKey = None
) -> JSONResponse:
    """분석(run_analysis)·문서 읽기(ingest_document)·변경 적용(apply_change) 작업을 만든다."""
    return idempotent(
        rt,
        p,
        idem,
        "POST /jobs",
        fingerprint(body.model_dump()),
        lambda: ops.create_job(p, body.type, body.params),
        headers_for=location,
    )


@router.get("", response_model=JobPage)
def list_jobs(
    ops: OpsDep,
    p: Viewer,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    status: str | None = None,
) -> Any:
    out = ops.list_jobs(p, decode_cursor(cursor), clamp_limit(limit), status)
    out["next_cursor"] = encode_cursor(out["next_cursor"]) if out["next_cursor"] else None
    return out


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, ops: OpsDep, p: Viewer) -> Any:
    return ops.job(p, job_id)


@router.post("/{job_id}/cancel", response_model=JobOut, status_code=202)
def cancel_job(job_id: str, ops: OpsDep, p: Member) -> Any:
    """대기 중이면 즉시 취소, 실행 중이면 취소 요청(작업 트랜잭션은 롤백된다)."""
    return ops.cancel_job(p, job_id)
