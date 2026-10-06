"""Liveness / readiness."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from jettae.api.deps import RuntimeDep
from jettae.api.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health() -> Any:
    """Process is up (no dependency checks)."""
    return {"status": "ok", "checks": {}}


@router.get("/ready", response_model=HealthOut, responses={503: {"model": HealthOut}})
def ready(rt: RuntimeDep) -> JSONResponse:
    """DB reachable, schema at the Alembic head, blob directory writable."""
    from jettae.db.migrate import current_revision, head_revision

    checks: dict[str, Any] = {}
    ok = True
    try:
        with rt.db.engine.connect() as c:
            c.execute(sa.text("SELECT 1"))
        checks["database"] = "ok"
        cur, head = current_revision(rt.db.engine), head_revision(rt.settings.migrations_dir)
        checks["migrations"] = {"current": cur, "head": head}
        if cur != head:
            ok = False
    except Exception as e:  # report, do not crash
        checks["database"] = f"error: {type(e).__name__}"
        ok = False
    checks["blob_store"] = "ok" if rt.blobs.writable() else "not writable"
    ok = ok and checks["blob_store"] == "ok"
    return JSONResponse(
        {"status": "ok" if ok else "unavailable", "checks": checks},
        status_code=200 if ok else 503,
    )
