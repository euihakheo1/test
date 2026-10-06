"""Typed JSON codec for domain records stored in JSON columns.

Encoding uses the canonical form of :mod:`jettae.domain.hashing` (tagged ``$money``,
``$date``, ``$dt``, ``$dec``, ``$type``), so a decoded object has the same canonical form —
and therefore the same content hash — as the original. Decoding is driven by dataclass
type hints (no pickle, no arbitrary class lookup): only the dataclasses registered in
:data:`KNOWN_TYPES` can be materialised from ``Any``-typed fields.

``CODEC_VERSION`` is stored as ``schema_version`` next to every payload.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from functools import cache
from typing import Any, Union, get_args, get_origin

from jettae.domain.hashing import to_canonical
from jettae.domain.models import (
    Agreement,
    Allocation,
    Approval,
    BankTxn,
    Computation,
    Decision,
    DocumentVersion,
    EvidenceLink,
    Fact,
    Invoice,
    SettlementLine,
    SourceSpan,
)
from jettae.domain.money import Money
from jettae.evidence.engine import GroupOutcome
from jettae.recon.matcher import ItemResult, PaymentResult
from jettae.verify.checks import CheckResult

CODEC_VERSION = 1

KNOWN_TYPES: dict[str, type[Any]] = {
    t.__name__: t
    for t in (
        Agreement,
        Allocation,
        Approval,
        BankTxn,
        CheckResult,
        Computation,
        Decision,
        DocumentVersion,
        EvidenceLink,
        Fact,
        GroupOutcome,
        Invoice,
        ItemResult,
        PaymentResult,
        SettlementLine,
        SourceSpan,
    )
}


class CodecError(ValueError):
    pass


def encode(obj: Any) -> Any:
    """JSON-compatible canonical form (raises on float / naive datetime)."""
    return to_canonical(obj)


@cache
def _hints(cls: type[Any]) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _tag(data: Any, tag: str) -> Any:
    if not (isinstance(data, Mapping) and tag in data):
        raise CodecError(f"expected {tag} object, got {data!r:.80}")
    return data[tag]


def decode_any(data: Any) -> Any:
    """Decode a value of unknown static type (lists become tuples, tags become objects)."""
    if data is None or isinstance(data, bool | int | str):
        return data
    if isinstance(data, float):
        raise CodecError("float in stored domain data")
    if isinstance(data, list):
        return tuple(decode_any(x) for x in data)
    if isinstance(data, Mapping):
        if "$money" in data:
            a, c = data["$money"]
            return Money(a, c)
        if "$date" in data:
            return date.fromisoformat(data["$date"])
        if "$dt" in data:
            return datetime.fromisoformat(data["$dt"])
        if "$dec" in data:
            return Decimal(data["$dec"])
        if "$type" in data:
            cls = KNOWN_TYPES.get(data["$type"])
            if cls is None:
                raise CodecError(f"unknown stored type {data['$type']!r}")
            return decode(cls, data)
        return {k: decode_any(v) for k, v in data.items()}
    raise CodecError(f"cannot decode {type(data).__name__}")


def decode(tp: Any, data: Any) -> Any:
    """Decode ``data`` (canonical JSON) into an instance of static type ``tp``."""
    if tp is Any or tp is object:
        return decode_any(data)
    origin = get_origin(tp)
    args = get_args(tp)
    if origin is Union or origin is types.UnionType:
        if data is None and type(None) in args:
            return None
        real = [a for a in args if a is not type(None)]
        if len(real) == 1:
            return decode(real[0], data)
        if isinstance(data, Mapping) and "$type" in data:
            for a in real:
                if isinstance(a, type) and a.__name__ == data["$type"]:
                    return decode(a, data)
        for a in real:  # scalars: first type that accepts the value
            try:
                return decode(a, data)
            except (CodecError, ValueError, TypeError):
                continue
        raise CodecError(f"no union member of {tp} accepts {data!r:.80}")
    if origin is typing.Literal:
        return data
    if tp is type(None):
        if data is not None:
            raise CodecError("expected null")
        return None
    if tp is bool:
        if not isinstance(data, bool):
            raise CodecError("expected bool")
        return data
    if tp is int:
        if isinstance(data, bool) or not isinstance(data, int):
            raise CodecError("expected int")
        return data
    if tp is str:
        if not isinstance(data, str):
            raise CodecError("expected str")
        return data
    if tp is float:
        raise CodecError("float fields are not stored")
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(data)
    if tp is Money:
        a, c = _tag(data, "$money")
        return Money(a, c)
    if tp is datetime:
        return datetime.fromisoformat(_tag(data, "$dt"))
    if tp is date:
        return date.fromisoformat(_tag(data, "$date"))
    if tp is Decimal:
        return Decimal(_tag(data, "$dec"))
    if origin is tuple:
        if not isinstance(data, list | tuple):
            raise CodecError(f"expected list for {tp}")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(decode(args[0], x) for x in data)
        if not args:
            return tuple(decode_any(x) for x in data)
        if len(args) != len(data):
            raise CodecError(f"tuple arity mismatch for {tp}")
        return tuple(decode(a, x) for a, x in zip(args, data, strict=True))
    if origin in (frozenset, set):
        return frozenset(decode(args[0] if args else Any, x) for x in data)
    if origin is list:
        return [decode(args[0] if args else Any, x) for x in data]
    if (
        origin in (dict, Mapping)
        or origin is getattr(typing, "Mapping", None)
        or (origin is not None and isinstance(origin, type) and issubclass(origin, Mapping))
    ):
        if not isinstance(data, Mapping):
            raise CodecError(f"expected object for {tp}")
        kt, vt = args if args else (str, Any)
        return {decode(kt, k): decode(vt, v) for k, v in data.items()}
    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        if not isinstance(data, Mapping):
            raise CodecError(f"expected object for {tp.__name__}")
        if data.get("$type") != tp.__name__:
            raise CodecError(f"expected $type {tp.__name__}, got {data.get('$type')!r}")
        hints = _hints(tp)
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(tp):
            if not f.init:
                continue
            if f.name not in data:
                if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                    raise CodecError(f"{tp.__name__}.{f.name} missing")
                continue
            kwargs[f.name] = decode(hints[f.name], data[f.name])
        return tp(**kwargs)
    raise CodecError(f"unsupported type {tp!r}")


def decode_as[T](cls: type[T], data: Any) -> T:
    return typing.cast(T, decode(cls, data))
