"""Korean business calendar: public holidays from ``holidays.KR`` + an override list.

The override file (JSON, or YAML if PyYAML is importable) adds temporary holidays
(임시공휴일) that the installed ``holidays`` package does not know yet, or removes entries::

    {"add": [{"date": "2026-06-03", "name": "임시공휴일", "source": "<url>"}],
     "remove": ["2026-01-01"]}
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import holidays as _holidays

from jettae.domain.dates import is_weekend
from jettae.domain.dates import next_business_day as _next_business_day
from jettae.domain.hashing import content_hash

DEFAULT_OVERRIDES = Path(__file__).with_name("kr_holiday_overrides.json")


def _load_mapping(path: Path) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("PyYAML is required for YAML holiday overrides") from e
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text) if text.strip() else {}
    if not isinstance(data, Mapping):
        raise ValueError(f"{path}: override file must be a mapping")
    return data


class KrCalendar:
    """Business-day calendar for Korea (weekends + public holidays + overrides)."""

    def __init__(
        self,
        add: Mapping[date, str] | None = None,
        remove: Iterable[date] = (),
        source: str = "holidays.KR",
    ) -> None:
        self._add = dict(add or {})
        self._remove = frozenset(remove)
        self._source = source
        self._years: dict[int, Mapping[date, str]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_override_file(cls, path: str | Path | None = None) -> KrCalendar:
        p = Path(path) if path is not None else DEFAULT_OVERRIDES
        if not p.exists():
            if path is not None:
                raise FileNotFoundError(p)
            return cls()
        data = _load_mapping(p)
        add: dict[date, str] = {}
        for entry in data.get("add", []) or []:
            if isinstance(entry, Mapping):
                add[date.fromisoformat(str(entry["date"]))] = str(entry.get("name", "override"))
            else:
                add[date.fromisoformat(str(entry))] = "override"
        remove = [date.fromisoformat(str(x)) for x in data.get("remove", []) or []]
        return cls(add=add, remove=remove, source=f"holidays.KR+{p.name}")

    def _year(self, year: int) -> Mapping[date, str]:
        with self._lock:
            if year not in self._years:
                self._years[year] = dict(_holidays.KR(years=year).items())
            return self._years[year]

    def holiday_name(self, d: date) -> str | None:
        if d in self._remove:
            return None
        if d in self._add:
            return self._add[d]
        return self._year(d.year).get(d)

    def is_holiday(self, d: date) -> bool:
        return self.holiday_name(d) is not None

    def is_business_day(self, d: date) -> bool:
        return not is_weekend(d) and not self.is_holiday(d)

    def next_business_day(self, d: date) -> date:
        return _next_business_day(d, self.is_holiday)

    def describe(self, d: date) -> str:
        name = self.holiday_name(d)
        if name:
            return f"공휴일({name})"
        if d.weekday() == 5:
            return "토요일"
        if d.weekday() == 6:
            return "일요일"
        return "영업일"

    def fingerprint(self) -> str:
        return content_hash(
            {
                "source": "holidays.KR",
                "lib_version": getattr(_holidays, "__version__", "?"),
                "add": {d.isoformat(): n for d, n in sorted(self._add.items())},
                "remove": sorted(d.isoformat() for d in self._remove),
            }
        )


_default: KrCalendar | None = None
_default_lock = threading.Lock()


def default_calendar() -> KrCalendar:
    global _default
    with _default_lock:
        if _default is None:
            _default = KrCalendar.from_override_file()
        return _default
