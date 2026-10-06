"""In-process throttling for unauthenticated auth endpoints.

- :class:`WindowLimiter`: at most ``limit`` requests per key per sliding window (per client IP
  for signup / login / refresh).
- :class:`FailureLimiter`: after ``max_failures`` failed logins for one key (normalised
  email) within ``window_s``, further attempts get 429 until the window passes. A successful
  login clears the key.

State lives in the API process memory: with several API processes (e.g. 2 uvicorn workers)
each keeps its own counters, so the effective limit is ``limit x processes``. For a hard,
shared limit put the reverse-proxy rule from ``docs/runbook.md`` in front of the API.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime

from jettae.api.errors import ApiError

MAX_KEYS = 50_000  # bound memory: oldest keys are dropped first


def _too_many(retry_after: float) -> ApiError:
    secs = max(1, math.ceil(retry_after))
    return ApiError(
        429,
        "too_many_requests",
        "too many attempts; try again later",
        {"retry_after_s": secs},
        headers={"Retry-After": str(secs)},
    )


class WindowLimiter:
    def __init__(self, limit: int, window_s: float, clock: Callable[[], datetime]) -> None:
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> None:
        if self.limit <= 0:
            return
        now = self.clock().timestamp()
        with self._lock:
            q = self._hits.pop(key, None) or deque()
            while q and q[0] <= now - self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                self._hits[key] = q
                raise _too_many(q[0] + self.window_s - now)
            q.append(now)
            self._hits[key] = q
            while len(self._hits) > MAX_KEYS:
                self._hits.pop(next(iter(self._hits)))


class FailureLimiter:
    def __init__(self, max_failures: int, window_s: float, clock: Callable[[], datetime]) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self.clock = clock
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._fails.pop(key, None) or deque()
        while q and q[0] <= now - self.window_s:
            q.popleft()
        return q

    def check(self, key: str) -> None:
        if self.max_failures <= 0:
            return
        now = self.clock().timestamp()
        with self._lock:
            q = self._prune(key, now)
            if q:
                self._fails[key] = q
            if len(q) >= self.max_failures:
                raise _too_many(q[0] + self.window_s - now)

    def fail(self, key: str) -> None:
        if self.max_failures <= 0:
            return
        now = self.clock().timestamp()
        with self._lock:
            q = self._prune(key, now)
            q.append(now)
            self._fails[key] = q
            while len(self._fails) > MAX_KEYS:
                self._fails.pop(next(iter(self._fails)))

    def succeed(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
