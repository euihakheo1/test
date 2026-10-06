"""Compatibility re-export: the settings moved to :mod:`jettae.config` (one place for the
environment contract, ``.env`` loading and the startup check)."""

from __future__ import annotations

from jettae.config import (
    _EPHEMERAL,
    DEFAULT_UPLOAD_TYPES,
    REPO_ROOT,
    Settings,
    get_settings,
    require_valid_environment,
)

__all__ = [
    "DEFAULT_UPLOAD_TYPES",
    "REPO_ROOT",
    "Settings",
    "_EPHEMERAL",
    "get_settings",
    "require_valid_environment",
]
