"""Agent investigations of one decision (web flow).

- ``GET  /investigations/capabilities``          -> ``{modes_enabled, live_enabled}``
- ``POST /decisions/{id}/investigations``        -> 202 ``{investigation_id, job_id}``
- ``GET  /decisions/{id}/investigations``        -> list, newest first

The tenant comes from the authenticated principal only. Whether paid model calls are
possible is a server setting (:func:`jettae.agents.investigations.live_capability`); a
request for ``mode: live`` on a server that does not allow it is accepted and finishes as
``refused`` in the worker (which is the process that would make the call), so the page has
one way to follow every investigation. Investigations never change engine results.

Mounted by ``jettae.api.main`` through ``OPTIONAL_ROUTERS`` (module attribute ``router``).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from jettae.agents.investigations import capabilities
from jettae.api.deps import IdemKey, Member, RuntimeDep, Viewer, fingerprint, idempotent
from jettae.api.errors import ERROR_RESPONSES
from jettae.db.repos_investigations import InvestigationView, SqlInvestigations
from jettae.db.runtime import Runtime
from jettae.domain.dates import ensure_utc
from jettae.domain.errors import NotFoundError

API = "/api/v1"
router = APIRouter(tags=["investigations"], responses=ERROR_RESPONSES)


class InvestigationCreate(BaseModel):
    # unknown keys (e.g. a tenant id or a budget sent by the page) are rejected
    model_config = ConfigDict(extra="forbid")
    strategy: Literal["single", "roles"] = "single"
    mode: Literal["offline", "replay", "live", "local"] = "offline"


def _repo(rt: Runtime) -> SqlInvestigations:
    return SqlInvestigations(rt.db, clock=rt.clock, max_attempts=min(rt.queue.max_attempts, 2))


def _ts(v: Any) -> str | None:
    return ensure_utc(v).isoformat() if v is not None else None


def investigation_out(v: InvestigationView) -> dict[str, Any]:
    return {
        "id": v.id,
        "decision_id": v.decision_id,
        "strategy": v.strategy,
        "mode": v.mode,
        "status": v.status,
        "findings": v.findings,
        "usage": v.usage,
        "error": v.error,
        "job_id": v.job_id,
        "decision_result_hash": v.decision_result_hash,
        "report": v.report,
        "created_at": _ts(v.created_at),
        "started_at": _ts(v.started_at),
        "finished_at": _ts(v.finished_at),
    }


@router.get("/investigations/capabilities")
def investigation_capabilities(p: Viewer) -> dict[str, Any]:
    """조사 방식별 사용 가능 여부. 실제 LLM 호출(live)은 서버 설정과 예산으로만 켜진다."""
    return capabilities()


@router.post(
    "/decisions/{decision_id}/investigations",
    status_code=202,
    responses={404: {"description": "decision not found (in this tenant)"}},
)
def start_investigation(
    decision_id: str,
    body: InvestigationCreate,
    rt: RuntimeDep,
    p: Member,
    idem: IdemKey = None,
) -> JSONResponse:
    """결정 하나를 Agent로 조사하는 작업을 만든다. 엔진 수치·승인은 바뀌지 않는다."""

    def run() -> tuple[int, Any]:
        dec = rt.repos.decisions.get_current(p.tenant_id, decision_id)
        if dec is None:
            raise NotFoundError(decision_id)
        v = _repo(rt).create(
            p.tenant_id,
            decision_id=decision_id,
            strategy=body.strategy,
            mode=body.mode,
            requested_by=p.email,
            decision_result_hash=dec.result_hash,
        )
        return 202, {
            "investigation_id": v.id,
            "job_id": v.job_id,
            "status": v.status,
            "status_url": f"{API}/jobs/{v.job_id}",
        }

    return idempotent(
        rt,
        p,
        idem,
        f"POST /decisions/{decision_id}/investigations",
        fingerprint(body.model_dump()),
        run,
        headers_for=lambda b: {"Location": b["status_url"]} if isinstance(b, dict) else {},
    )


@router.get("/decisions/{decision_id}/investigations")
def list_investigations(
    decision_id: str,
    rt: RuntimeDep,
    p: Viewer,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict[str, Any]]:
    """이 결정의 조사 기록(최신순). 다른 회사의 결정 id는 없는 id와 같다."""
    items = _repo(rt).list_for_decision(p.tenant_id, decision_id, limit=limit)
    if not items and rt.repos.decisions.get_current(p.tenant_id, decision_id) is None:
        raise NotFoundError(decision_id)
    return [investigation_out(v) for v in items]
