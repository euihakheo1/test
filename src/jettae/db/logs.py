"""Structured (JSON lines) logging for the API and worker processes."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in vars(record).items():
            if k not in _RESERVED and not k.startswith("_"):
                out[k] = v if isinstance(v, str | int | bool | type(None)) else str(v)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def configure_logging(level: str = "INFO", *, json_lines: bool = True) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(
        JsonFormatter()
        if json_lines
        else logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    root.addHandler(h)
    root.setLevel(level.upper())
    logging.getLogger("alembic").setLevel(logging.WARNING)
