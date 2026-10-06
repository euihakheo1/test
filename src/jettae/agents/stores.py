"""Tenant-scoped stores for agent runs and proposals (drafts that a person must act on).

In-memory by default; with ``root`` the data is also written as JSON files under
``<root>/<sha256(tenant)[:16]>/`` so the CLI and the MCP server see the same runs. Nothing
here is sent anywhere: a "missing evidence request" is a draft for the user.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jettae.llm.replay import tenant_slug


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _safe_id(s: str) -> str:
    if not s or len(s) > 80 or any(not (c.isalnum() or c in "_-") for c in s):
        raise ValueError(f"invalid id {s!r}")
    return s


class AgentStore:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else None
        self._lock = threading.RLock()
        self._runs: dict[tuple[str, str], dict[str, Any]] = {}
        self._proposals: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------ runs
    def save_run(self, tenant_id: str, run_id: str, data: Mapping[str, Any]) -> None:
        rid = _safe_id(run_id)
        doc = {**json.loads(json.dumps(dict(data), default=str)), "tenant_id": tenant_id}
        with self._lock:
            self._runs[(tenant_id, rid)] = doc
            if self.root is not None:
                p = self.root / tenant_slug(tenant_id) / "runs" / f"{rid}.json"
                _atomic_write(p, json.dumps(doc, ensure_ascii=False, indent=1))

    def get_run(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        try:
            rid = _safe_id(run_id)
        except ValueError:
            return None
        with self._lock:
            doc = self._runs.get((tenant_id, rid))
            if doc is None and self.root is not None:
                p = self.root / tenant_slug(tenant_id) / "runs" / f"{rid}.json"
                if p.exists():
                    doc = json.loads(p.read_text(encoding="utf-8"))
            if doc is None or doc.get("tenant_id") != tenant_id:
                return None
            return dict(doc)

    # ------------------------------------------------------------------ proposals
    def add_proposal(self, tenant_id: str, proposal: Mapping[str, Any]) -> dict[str, Any]:
        doc = {**json.loads(json.dumps(dict(proposal), default=str)), "tenant_id": tenant_id}
        with self._lock:
            self._proposals.setdefault(tenant_id, []).append(doc)
            if self.root is not None:
                p = self.root / tenant_slug(tenant_id) / "proposals.jsonl"
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(doc, ensure_ascii=False) + "\n")
        return doc

    def proposals(self, tenant_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._proposals.get(tenant_id, []))
        return [p for p in items if run_id is None or p.get("run_id") == run_id]
