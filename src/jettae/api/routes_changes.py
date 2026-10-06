"""/changes: new payments / corrections -> apply_change job -> impact plan in the job result."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from jettae.api.deps import IdemKey, Member, RuntimeDep, fingerprint, get_ops, idempotent
from jettae.api.errors import ERROR_RESPONSES
from jettae.api.routes_jobs import location
from jettae.api.schemas import ChangesRequest, JobAccepted
from jettae.api.uploads import check_upload

router = APIRouter(prefix="/changes", tags=["changes"], responses=ERROR_RESPONSES)
OpsDep = Annotated[Any, Depends(get_ops)]


@router.post("", status_code=202, response_model=JobAccepted)
def submit_changes(
    body: ChangesRequest, rt: RuntimeDep, ops: OpsDep, p: Member, idem: IdemKey = None
) -> JSONResponse:
    """기록 단위 변경(새 입금, 정정, 약정 추가·삭제). 작업 결과에 영향 범위(plan)가 담긴다."""
    return idempotent(
        rt,
        p,
        idem,
        "POST /changes",
        fingerprint(body.model_dump()),
        lambda: ops.submit_changes(p, body.changes),
        headers_for=location,
    )


@router.post("/upload", status_code=202)
async def upload_change(
    rt: RuntimeDep,
    ops: OpsDep,
    p: Member,
    file: Annotated[UploadFile, File()],
    kind: Annotated[str, Form()] = "other",
    document_id: Annotated[str | None, Form(description="정정 대상 문서 ID")] = None,
    idem: IdemKey = None,
) -> JSONResponse:
    """정정 문서·새 입금 내역 파일을 올린다 -> 읽기 + 증분 재계산 작업(202)."""
    up = await check_upload(
        file, max_bytes=rt.settings.max_upload_bytes, allowed_ext=rt.settings.upload_extensions
    )

    def run() -> tuple[int, Any]:
        return ops.upload_change(  # type: ignore[no-any-return]
            p,
            filename=up.filename,
            media_type=up.media_type,
            content=up.content,
            kind=kind,
            document_id=document_id,
        )

    fp = fingerprint(up.content, up.filename, kind, document_id)
    return await run_in_threadpool(
        idempotent, rt, p, idem, "POST /changes/upload", fp, run, location
    )
