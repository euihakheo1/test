"""/documents, /document-versions: upload, list, versions, spans, content, column mapping."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

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
    AcknowledgeRequest,
    DocumentHeadOut,
    DocumentPage,
    DocumentVersionOut,
    MappingConfirm,
    MappingConfirmed,
    MappingOut,
    SpanPage,
    UploadResponse,
    VersionList,
)
from jettae.api.uploads import check_upload

router = APIRouter(tags=["documents"], responses=ERROR_RESPONSES)
OpsDep = Annotated[Any, Depends(get_ops)]


@router.post(
    "/documents",
    status_code=201,
    response_model=UploadResponse,
    responses={200: {"model": UploadResponse, "description": "identical file already stored"}},
)
async def upload_document(
    rt: RuntimeDep,
    ops: OpsDep,
    p: Member,
    file: Annotated[UploadFile, File(description="CSV / XLSX / text PDF")],
    kind: Annotated[str, Form()] = "other",
    document_id: Annotated[str | None, Form(description="새 버전으로 올릴 문서 ID")] = None,
    auto_ingest: Annotated[bool, Form()] = True,
    idem: IdemKey = None,
) -> JSONResponse:
    """문서를 올린다(크기·형식 검사, 회사별 내용 주소 저장). 같은 내용은 한 번만 저장된다."""
    up = await check_upload(
        file, max_bytes=rt.settings.max_upload_bytes, allowed_ext=rt.settings.upload_extensions
    )

    def run() -> tuple[int, Any]:
        return ops.upload(  # type: ignore[no-any-return]
            p,
            filename=up.filename,
            media_type=up.media_type,
            content=up.content,
            kind=kind,
            document_id=document_id,
            auto_ingest=auto_ingest,
        )

    fp = fingerprint(up.content, up.filename, kind, document_id, auto_ingest)
    return await run_in_threadpool(idempotent, rt, p, idem, "POST /documents", fp, run)


@router.get("/documents", response_model=DocumentPage)
def list_documents(
    ops: OpsDep,
    p: Viewer,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> Any:
    return ops.list_documents(p, cursor, clamp_limit(limit))


@router.get("/documents/{document_id}/versions", response_model=VersionList)
def document_versions(document_id: str, ops: OpsDep, p: Viewer) -> Any:
    return ops.versions(p, document_id)


@router.get("/document-versions/{doc_version_id}", response_model=DocumentVersionOut)
def document_version(doc_version_id: str, ops: OpsDep, p: Viewer) -> Any:
    return ops.version(p, doc_version_id)


@router.get("/document-versions/{doc_version_id}/spans", response_model=SpanPage)
def document_spans(
    doc_version_id: str,
    ops: OpsDep,
    p: Viewer,
    cursor: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> Any:
    """이 문서 버전에서 추출된 사실과 원문 위치(시트·행·열 또는 페이지·문자 범위)."""
    return ops.spans(p, doc_version_id, cursor, clamp_limit(limit))


@router.get(
    "/document-versions/{doc_version_id}/content",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
def document_content(doc_version_id: str, ops: OpsDep, p: Viewer) -> Response:
    data, d = ops.content(p, doc_version_id)
    disposition = f"attachment; filename=\"{d.id}\"; filename*=UTF-8''{quote(d.filename, safe='')}"
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.post(
    "/document-versions/{doc_version_id}/acknowledge",
    response_model=DocumentHeadOut,
)
def acknowledge_document(
    doc_version_id: str, body: AcknowledgeRequest, ops: OpsDep, p: Member
) -> Any:
    """반영하지 못한 행·합계 차이를 확인했다고 기록한다(현재 버전, 같은 읽기 결과일 때만).
    확인 전에는 이 문서를 근거로 한 결과를 승인할 수 없다."""
    return ops.acknowledge(p, doc_version_id, body.fingerprint)


@router.get("/document-versions/{doc_version_id}/mapping", response_model=MappingOut)
def get_mapping(doc_version_id: str, ops: OpsDep, p: Viewer) -> Any:
    """제안된 열 매핑(수집 모듈)과 마지막으로 확인된 매핑."""
    return ops.get_mapping(p, doc_version_id)


@router.post(
    "/document-versions/{doc_version_id}/mapping",
    status_code=201,
    response_model=MappingConfirmed,
)
def confirm_mapping(
    doc_version_id: str,
    body: MappingConfirm,
    rt: RuntimeDep,
    ops: OpsDep,
    p: Member,
    idem: IdemKey = None,
) -> JSONResponse:
    """열 매핑을 확인하고(기록 보존) 필요하면 다시 읽기 작업을 만든다."""
    return idempotent(
        rt,
        p,
        idem,
        f"POST /document-versions/{doc_version_id}/mapping",
        fingerprint(body.model_dump()),
        lambda: ops.confirm_mapping(p, doc_version_id, body.mapping, body.reingest),
    )
