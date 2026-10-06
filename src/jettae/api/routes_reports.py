"""/reports/export: CSV / HTML with validity re-check at export time."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from jettae.api.deps import Viewer, get_ops
from jettae.api.errors import ERROR_RESPONSES
from jettae.api.schemas import ReportRequest

router = APIRouter(prefix="/reports", tags=["reports"], responses=ERROR_RESPONSES)
OpsDep = Annotated[Any, Depends(get_ops)]

HTML_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


@router.post(
    "/export",
    response_class=Response,
    responses={
        200: {"content": {"text/csv": {}, "text/html": {}}},
        409: {"description": "require_approved and some decisions are not currently approved"},
    },
)
def export_report(body: ReportRequest, ops: OpsDep, p: Viewer) -> Response:
    """보고서를 내보낸다. 내보내는 시점에 각 결과의 현재 유효성(승인 여부)을 다시 확인한다."""
    data, media, filename = ops.export(p, body.decision_ids, body.format, body.require_approved)
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }
    if body.format == "html":
        headers["Content-Security-Policy"] = HTML_CSP
    return Response(data, media_type=media, headers=headers)
