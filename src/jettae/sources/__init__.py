"""Real public data sources (fetch, manifest, convert). See docs/SPEC.md §3.

Shared helpers only: repository paths, manifests (URL, fetch time, file hash, license,
access failures) and idempotent result sections in ``docs/eval_results.md``.
Nothing here fabricates data: a failed fetch is recorded as a failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USER_AGENT = "jettae/0.1 (public-data research client)"


def repo_root() -> Path:
    """Repository root (``JETTAE_REPO_ROOT`` overrides; else derived from this file)."""
    env = os.environ.get("JETTAE_REPO_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    # src/jettae/sources/__init__.py -> repo root is parents[3]
    cand = here.parents[3]
    if (cand / "pyproject.toml").exists():
        return cand
    return Path.cwd()


def data_dir() -> Path:
    env = os.environ.get("JETTAE_DATA_DIR")
    return Path(env) if env else repo_root() / "data"


def raw_dir(source: str) -> Path:
    p = data_dir() / "raw" / source
    p.mkdir(parents=True, exist_ok=True)
    return p


def manifests_dir() -> Path:
    p = data_dir() / "manifests"
    p.mkdir(parents=True, exist_ok=True)
    return p


def results_dir() -> Path:
    p = data_dir() / "results"
    p.mkdir(parents=True, exist_ok=True)
    return p


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


_OC_RE = re.compile(r"([?&]OC=)([^&]+)")


def redact_url(url: str) -> str:
    """Hide a personal DRF OC key in URLs written to manifests (the public sample key
    ``test`` is kept so the URL stays reproducible)."""
    return _OC_RE.sub(lambda m: m.group(1) + (m.group(2) if m.group(2) == "test" else "***"), url)


def write_json(path: Path, obj: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def upsert_md_section(path: Path, key: str, body: str, title: str = "# Evaluation results") -> Path:
    """Insert or replace the block ``<!-- key:start --> ... <!-- key:end -->`` in ``path``.

    Blocks with other keys (written by other evaluation commands) are left untouched; a missing
    file is created.
    """
    start, end = f"<!-- {key}:start -->", f"<!-- {key}:end -->"
    block = f"{start}\n{body.rstrip()}\n{end}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else f"{title}\n\n"
    if start in text and end in text:
        i, j = text.index(start), text.index(end) + len(end)
        if j < len(text) and text[j] == "\n":
            j += 1
        text = text[:i] + block + text[j:]
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += "\n" + block
    path.write_text(text, encoding="utf-8")
    return path


def eval_results_md() -> Path:
    return repo_root() / "docs" / "eval_results.md"
