"""Record/replay cache for LLM responses.

The key (:meth:`LLMRequest.replay_key`) covers tenant, ``as_of``, the input hash (text and
image sha256), the model, the prompt hash (system prompt + schema + prompt version) and the
tool versions. A recorded response is replayed only for that exact combination, so a new
prompt, model, tool version or input never reuses an old answer.

Entries live under ``<root>/<sha256(tenant)[:16]>/<key[:2]>/<key>.json`` -- one directory
per tenant, so a cache directory can be deleted or exported per tenant. Recorded responses
of tenant data are tenant data: keep the cache directory with the same protection as the DB.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from jettae.llm.base import LLMRequest, TokenUsage


class ReplayStore(Protocol):
    def get(self, tenant_id: str, key: str) -> Mapping[str, Any] | None: ...
    def put(self, tenant_id: str, key: str, entry: Mapping[str, Any]) -> None: ...


def tenant_slug(tenant_id: str) -> str:
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:16]


class FileReplayStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def _path(self, tenant_id: str, key: str) -> Path:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("replay key must be a sha256 hex digest")
        return self.root / tenant_slug(tenant_id) / key[:2] / f"{key}.json"

    def get(self, tenant_id: str, key: str) -> Mapping[str, Any] | None:
        p = self._path(tenant_id, key)
        if not p.exists():
            return None
        entry = json.loads(p.read_text(encoding="utf-8"))
        if entry.get("tenant_id") != tenant_id or entry.get("key") != key:
            return None  # never serve another tenant's entry (slug collision / tampering)
        return entry

    def put(self, tenant_id: str, key: str, entry: Mapping[str, Any]) -> None:
        p = self._path(tenant_id, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(dict(entry), ensure_ascii=False, indent=1, sort_keys=True)
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(data)
                os.replace(tmp, p)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise


class MemoryReplayStore:
    def __init__(self) -> None:
        self._d: dict[tuple[str, str], dict[str, Any]] = {}

    def get(self, tenant_id: str, key: str) -> Mapping[str, Any] | None:
        return self._d.get((tenant_id, key))

    def put(self, tenant_id: str, key: str, entry: Mapping[str, Any]) -> None:
        self._d[(tenant_id, key)] = dict(entry)

    def __len__(self) -> int:
        return len(self._d)


def make_entry(
    request: LLMRequest,
    *,
    key: str,
    provider: str,
    model: str,
    raw_text: str,
    data: Mapping[str, Any],
    usage: TokenUsage,
    run_id: str,
    cost_krw: str | None,
) -> dict[str, Any]:
    return {
        "key": key,
        "tenant_id": request.tenant_id,
        "as_of": request.as_of.isoformat() if request.as_of else None,
        "purpose": request.purpose,
        "prompt_id": request.prompt_id,
        "prompt_version": request.prompt_version,
        "prompt_hash": request.prompt_hash,
        "input_hash": request.input_hash,
        "tool_versions": dict(sorted(request.tool_versions.items())),
        "provider": provider,
        "model": model,
        "raw_text": raw_text,
        "data": json.loads(json.dumps(data, default=str)),
        "usage": usage.to_json(),
        "run_id": run_id,
        "cost_krw": cost_krw,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
