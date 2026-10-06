"""Common error schema and exception mapping for the HTTP API.

Every error response has the shape::

    {"error": {"code": "...", "message": "...", "details": {...}, "request_id": "..."}}
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from jettae.app.contracts import MappingContractError
from jettae.app.doc_apply import DocumentAckRequiredError, DocumentStateError
from jettae.app.services import EvidenceLinkError
from jettae.db.ingest_bridge import IngestUnavailable
from jettae.db.plain import PlainError
from jettae.domain.errors import (
    DomainValidationError,
    NotFoundError,
    ReportValidityError,
    StaleResultError,
    TenantMismatchError,
)

log = logging.getLogger("jettae.api")


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


def error_response(
    request: Request,
    status: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    body = {"error": {"code": code, "message": message, "details": details, "request_id": rid}}
    return JSONResponse(body, status_code=status, headers=headers)


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "too_many_requests",
}

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    s: {"model": ErrorResponse, "description": c} for s, c in _HTTP_CODES.items()
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(request, exc.status, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        msg = exc.detail if isinstance(exc.detail, str) else code
        return error_response(
            request, exc.status_code, code, msg, headers=dict(exc.headers) if exc.headers else None
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errs = [
            {
                "loc": [str(x) for x in e.get("loc", ())],
                "msg": e.get("msg", ""),
                "type": e.get("type"),
            }
            for e in exc.errors()
        ]
        return error_response(
            request, 422, "validation_error", "request validation failed", {"errors": errs}
        )

    @app.exception_handler(PlainError)
    async def _plain(request: Request, exc: PlainError) -> JSONResponse:
        return error_response(
            request, 422, "validation_error", exc.message, {"path": exc.path or "$"}
        )

    @app.exception_handler(NotFoundError)
    async def _nf(request: Request, exc: NotFoundError) -> JSONResponse:
        return error_response(request, 404, "not_found", "resource not found")

    @app.exception_handler(TenantMismatchError)
    async def _tm(request: Request, exc: TenantMismatchError) -> JSONResponse:
        # never reveal that an object exists in another tenant
        return error_response(request, 404, "not_found", "resource not found")

    @app.exception_handler(StaleResultError)
    async def _stale(request: Request, exc: StaleResultError) -> JSONResponse:
        return error_response(
            request,
            409,
            "stale_result",
            "the decision changed since it was reviewed; reload and review again",
            {
                "decision_id": exc.decision_id,
                "expected_result_hash": exc.expected,
                "current_result_hash": exc.current,
            },
        )

    @app.exception_handler(ReportValidityError)
    async def _rv(request: Request, exc: ReportValidityError) -> JSONResponse:
        return error_response(
            request,
            409,
            "report_not_valid",
            "some decisions are not currently approved",
            {"invalid": exc.invalid},
        )

    @app.exception_handler(DocumentAckRequiredError)
    async def _ack(request: Request, exc: DocumentAckRequiredError) -> JSONResponse:
        return error_response(
            request,
            409,
            "document_ack_required",
            "the decision cites a document with excluded rows or total differences that "
            "nobody acknowledged; review the document first",
            {"decision_id": exc.decision_id, "documents": exc.documents},
        )

    @app.exception_handler(DocumentStateError)
    async def _docstate(request: Request, exc: DocumentStateError) -> JSONResponse:
        return error_response(request, 409, exc.code, str(exc))

    @app.exception_handler(MappingContractError)
    async def _mapping(request: Request, exc: MappingContractError) -> JSONResponse:
        return error_response(request, 422, "invalid_mapping", str(exc))

    @app.exception_handler(EvidenceLinkError)
    async def _elink(request: Request, exc: EvidenceLinkError) -> JSONResponse:
        return error_response(request, 422, exc.code, str(exc))

    @app.exception_handler(DomainValidationError)
    async def _dv(request: Request, exc: DomainValidationError) -> JSONResponse:
        return error_response(request, 422, "domain_validation_error", str(exc))

    @app.exception_handler(IngestUnavailable)
    async def _ing(request: Request, exc: IngestUnavailable) -> JSONResponse:
        return error_response(request, 503, "ingest_unavailable", str(exc))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception(
            "unhandled error", extra={"request_id": getattr(request.state, "request_id", None)}
        )
        return error_response(request, 500, "internal_error", "internal server error")
