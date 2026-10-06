"""Business-date helpers. Business dates are ``datetime.date``; system time is UTC-aware."""

from __future__ import annotations

import calendar
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

try:
    DISPLAY_TZ: ZoneInfo | None = ZoneInfo("Asia/Seoul")
except Exception:  # pragma: no cover - tzdata missing
    DISPLAY_TZ = None


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed for system time; use UTC-aware datetime")
    return dt.astimezone(UTC)


def to_display(dt: datetime) -> datetime:
    """Convert a system time to the display time zone (Asia/Seoul)."""
    if DISPLAY_TZ is None:  # pragma: no cover
        raise RuntimeError("tzdata for Asia/Seoul is not available")
    return ensure_utc(dt).astimezone(DISPLAY_TZ)


def parse_date(value: str | date) -> date:
    if isinstance(value, datetime):
        raise TypeError("expected a business date, got datetime")
    if isinstance(value, date):
        return value
    return date.fromisoformat(value.strip())


def add_days(base: date, days: int) -> date:
    """End of the period "N days from ``base``" under Korean civil-law counting.

    민법 제157조: the initial day is not counted, so the period runs from the day after
    ``base`` and its last day is ``base + N``.
    """
    return base + timedelta(days=days)


def days_between(start: date, end: date) -> int:
    """Signed number of days from ``start`` to ``end``."""
    return (end - start).days


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Saturday=5, Sunday=6


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def next_business_day(d: date, is_holiday: Callable[[date], bool]) -> date:
    """``d`` itself if it is a business day, otherwise the next business day."""
    cur = d
    for _ in range(370):
        if not is_weekend(cur) and not is_holiday(cur):
            return cur
        cur += timedelta(days=1)
    raise ValueError(f"no business day found within a year after {d}")
