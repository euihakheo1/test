"""Data locations for the FTC source. ``JETTAE_DATA_DIR`` overrides the repo ``data/`` dir."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def data_dir() -> Path:
    env = os.environ.get("JETTAE_DATA_DIR")
    return Path(env) if env else REPO_ROOT / "data"


def raw_dir() -> Path:
    return data_dir() / "raw" / "ftc"


def manifest_dir() -> Path:
    return data_dir() / "manifests"


def seeds_dir() -> Path:
    return data_dir() / "seeds"


def results_dir() -> Path:
    return data_dir() / "results"


def long_path(p: Path) -> Path:
    """On Windows, prefix long absolute paths with the extended-length prefix.

    MAX_PATH is 260 and LongPathsEnabled=0 on the dev machine (see AGENTS.md).
    """
    if os.name != "nt":
        return p
    s = str(p.absolute())
    prefix = "\\\\?\\"
    if len(s) < 240 or s.startswith(prefix):
        return p
    return Path(prefix + s.replace("/", "\\"))
