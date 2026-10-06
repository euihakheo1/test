"""Plain (client-facing) JSON <-> domain objects.

Plain form: Money -> ``{"amount": int, "currency": "KRW"}`` (an int is accepted as KRW on
input), dates/datetimes -> ISO strings, Decimal -> string, enums -> value, dataclasses ->
objects, tuples/sets -> arrays. Floats are rejected on input (AGENTS rule 3).

Used by the API (request validation, responses) and the worker (job payloads).
"""

from __future__ import annotations

import dataclasses
import types
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Union, get_args, get_origin

from jettae.db.codec import _hints
from jettae.domain.models import Agreement, BankTxn, Change, Fact, Invoice, SettlementLine
from jettae.domain.money import Money
from jettae.domain.status import ChangeKind

CHANGE_ENTITIES: dict[str, type[Any]] = {
    "invoice": Invoice,
    "settlement_line": SettlementLine,
    "bank_txn": BankTxn,
    "agreement": Agreement,
    "fact": Fact,
}


class PlainError(ValueError):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path or '$'}: {message}")
        self.path = path
        self.message = message


# ----------------------------------------------------------------------------- output
def to_plain(obj: Any) -> Any:
    if obj is None or isinstance(obj, bool | int | str):
        return obj
    if isinstance(obj, float):
        raise TypeError("float in domain output")
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Money):
        return {"amount": obj.amount, "currency": obj.currency}
    if isinstance(obj, Decimal):
        return format(obj.normalize(), "f")
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, set | frozenset):
        return sorted((to_plain(x) for x in obj), key=str)
    if isinstance(obj, list | tuple):
        return [to_plain(x) for x in obj]
    raise TypeError(f"cannot convert {type(obj).__name__} to JSON")


# ----------------------------------------------------------------------------- input
def from_plain(tp: Any, data: Any, path: str = "") -> Any:
    if isinstance(data, float):
        raise PlainError(path, "float values are not allowed; use integers (won) or strings")
    if tp is Any or tp is object:
        return _any(data, path)
    origin, args = get_origin(tp), get_args(tp)
    if origin is Union or origin is types.UnionType:
        if data is None:
            if type(None) in args:
                return None
            raise PlainError(path, "value required")
        real = [a for a in args if a is not type(None)]
        errors = []
        for a in real:
            try:
                return from_plain(a, data, path)
            except PlainError as e:
                errors.append(e.message)
        raise PlainError(path, " / ".join(errors))
    if data is None:
        raise PlainError(path, "value required")
    if tp is bool:
        if not isinstance(data, bool):
            raise PlainError(path, "expected boolean")
        return data
    if tp is int:
        if isinstance(data, bool) or not isinstance(data, int):
            raise PlainError(path, "expected integer")
        return data
    if tp is str:
        if not isinstance(data, str):
            raise PlainError(path, "expected string")
        return data
    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(data)
        except ValueError:
            allowed = ", ".join(str(m.value) for m in tp)  # type: ignore[attr-defined]
            raise PlainError(path, f"expected one of: {allowed}") from None
    if tp is Money:
        try:
            if isinstance(data, int) and not isinstance(data, bool):
                return Money(data)
            if isinstance(data, Mapping):
                return Money(data["amount"], data.get("currency", "KRW"))
        except (KeyError, ValueError, TypeError) as e:
            raise PlainError(path, f"invalid money: {e}") from None
        raise PlainError(path, "expected integer won amount or {amount, currency}")
    if tp is datetime:
        try:
            v = datetime.fromisoformat(data)
        except (TypeError, ValueError):
            raise PlainError(path, "expected ISO datetime") from None
        if v.tzinfo is None:
            raise PlainError(path, "datetime must include a UTC offset")
        return v
    if tp is date:
        if not isinstance(data, str) or len(data) != 10:
            raise PlainError(path, "expected date YYYY-MM-DD")
        try:
            return date.fromisoformat(data)
        except ValueError:
            raise PlainError(path, "expected date YYYY-MM-DD") from None
    if tp is Decimal:
        try:
            return Decimal(str(data))
        except InvalidOperation:
            raise PlainError(path, "expected decimal string") from None
    if origin is tuple:
        if not isinstance(data, list):
            raise PlainError(path, "expected array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(from_plain(args[0], x, f"{path}[{i}]") for i, x in enumerate(data))
        if len(args) != len(data):
            raise PlainError(path, f"expected {len(args)} items")
        return tuple(
            from_plain(a, x, f"{path}[{i}]")
            for i, (a, x) in enumerate(zip(args, data, strict=True))
        )
    if origin in (frozenset, set):
        if not isinstance(data, list):
            raise PlainError(path, "expected array")
        return frozenset(from_plain(args[0], x, f"{path}[{i}]") for i, x in enumerate(data))
    if origin is not None and isinstance(origin, type) and issubclass(origin, Mapping):
        if not isinstance(data, Mapping):
            raise PlainError(path, "expected object")
        kt, vt = args if args else (str, Any)
        return {from_plain(kt, k, path): from_plain(vt, v, f"{path}.{k}") for k, v in data.items()}
    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        if not isinstance(data, Mapping):
            raise PlainError(path, f"expected object ({tp.__name__})")
        hints = _hints(tp)
        names = {f.name for f in dataclasses.fields(tp) if f.init}
        unknown = sorted(set(data) - names)
        if unknown:
            raise PlainError(path, f"unknown fields: {', '.join(unknown)}")
        kwargs = {}
        for f in dataclasses.fields(tp):
            if not f.init or f.name not in data:
                continue
            kwargs[f.name] = from_plain(hints[f.name], data[f.name], f"{path}.{f.name}")
        try:
            return tp(**kwargs)
        except TypeError as e:
            raise PlainError(path, f"invalid {tp.__name__}: {e}") from None
        except ValueError as e:
            raise PlainError(path, str(e)) from None
    raise PlainError(path, f"unsupported type {tp!r}")


def _any(data: Any, path: str) -> Any:
    if isinstance(data, float):
        raise PlainError(path, "float values are not allowed")
    if isinstance(data, list):
        return tuple(_any(x, f"{path}[{i}]") for i, x in enumerate(data))
    if isinstance(data, Mapping):
        return {str(k): _any(v, f"{path}.{k}") for k, v in data.items()}
    return data


def record_from_plain(entity: str, data: Mapping[str, Any], tenant_id: str, path: str = "") -> Any:
    """Build a ledger record / fact. ``tenant_id`` always comes from the authenticated
    principal; a client-supplied ``tenant_id`` is ignored."""
    typ = CHANGE_ENTITIES.get(entity)
    if typ is None:
        raise PlainError(path, f"unknown entity {entity!r}")
    if not isinstance(data, Mapping):
        raise PlainError(path, "expected object")
    body = {k: v for k, v in data.items() if k != "tenant_id"}
    body["tenant_id"] = tenant_id
    return from_plain(typ, body, path)


def changes_from_plain(items: Any, tenant_id: str) -> list[Change]:
    """``[{"kind": "add|update|remove", "entity": ..., "id": ..., "record": {...}}]``."""
    if not isinstance(items, list) or not items:
        raise PlainError("changes", "expected a non-empty array")
    out: list[Change] = []
    for i, it in enumerate(items):
        p = f"changes[{i}]"
        if not isinstance(it, Mapping):
            raise PlainError(p, "expected object")
        kind = from_plain(ChangeKind, it.get("kind"), f"{p}.kind")
        entity = it.get("entity")
        if entity not in CHANGE_ENTITIES:
            raise PlainError(f"{p}.entity", f"expected one of: {', '.join(CHANGE_ENTITIES)}")
        rid = it.get("id")
        if not isinstance(rid, str) or not rid or len(rid) > 128:
            raise PlainError(f"{p}.id", "expected non-empty string (max 128)")
        if kind is ChangeKind.REMOVE:
            out.append(Change(kind, entity, rid))
            continue
        rec = record_from_plain(entity, it.get("record") or {}, tenant_id, f"{p}.record")
        if rec.id != rid:
            raise PlainError(f"{p}.record.id", "must equal the change id")
        out.append(Change(kind, entity, rid, rec))
    return out


def changes_to_payload(changes: list[Change]) -> list[dict[str, Any]]:
    """Inverse of :func:`changes_from_plain` (tenant_id dropped; re-derived on decode)."""
    out = []
    for c in changes:
        item: dict[str, Any] = {"kind": c.kind.value, "entity": c.entity, "id": c.entity_id}
        if c.record is not None:
            rec = to_plain(c.record)
            rec.pop("tenant_id", None)
            item["record"] = rec
        out.append(item)
    return out


__all__ = [
    "CHANGE_ENTITIES",
    "PlainError",
    "changes_from_plain",
    "changes_to_payload",
    "from_plain",
    "record_from_plain",
    "to_plain",
]
