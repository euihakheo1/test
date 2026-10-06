"""Canonical JSON encoding and content hashes for domain objects (deterministic, no float)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from jettae.domain.money import Money


def to_canonical(obj: Any) -> Any:
    """Convert ``obj`` into a JSON-compatible structure with a stable representation."""
    if obj is None or isinstance(obj, bool | int | str):
        return obj
    if isinstance(obj, float):
        raise TypeError("float is not allowed in canonical domain data")
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Money):
        return {"$money": [obj.amount, obj.currency]}
    if isinstance(obj, Decimal):
        return {"$dec": format(obj.normalize(), "f")}
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            raise TypeError("naive datetime in canonical data")
        return {"$dt": obj.isoformat()}
    if isinstance(obj, date):
        return {"$date": obj.isoformat()}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {"$type": type(obj).__name__}
        for f in dataclasses.fields(obj):
            out[f.name] = to_canonical(getattr(obj, f.name))
        return out
    if isinstance(obj, Mapping):
        return {str(k): to_canonical(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, set | frozenset):
        items = [to_canonical(x) for x in obj]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
    if isinstance(obj, list | tuple):
        return [to_canonical(x) for x in obj]
    raise TypeError(f"cannot canonicalize {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(to_canonical(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(obj: Any) -> str:
    """sha256 hex of the canonical JSON of ``obj``."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
