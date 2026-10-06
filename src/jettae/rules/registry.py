"""Rule version registry with valid time (effective dates) and known time (known_from)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from jettae.domain.errors import DomainValidationError, RuleNotFoundError
from jettae.domain.hashing import content_hash


@dataclass(frozen=True)
class RuleVersion:
    """One version of a rule.

    ``effective_from is None`` means the version is registered but INACTIVE (e.g. an
    amendment whose enforcement date is not fixed yet). ``known_from`` is the date the
    version became known (e.g. promulgation / passage), used for ``as known at`` queries.
    """

    rule_id: str
    version: str
    effective_from: date | None
    effective_to: date | None
    known_from: date | None
    source_url: str
    params: Mapping[str, Any] = field(default_factory=dict)
    title: str = ""
    source_note: str = ""
    verified: bool = False

    @property
    def key(self) -> str:
        return f"{self.rule_id}@{self.version}"

    @property
    def active(self) -> bool:
        return self.effective_from is not None

    def applies_on(self, on: date, known_at: date | None = None) -> bool:
        if self.effective_from is None:
            return False
        if on < self.effective_from:
            return False
        if self.effective_to is not None and on > self.effective_to:
            return False
        return not (
            known_at is not None and self.known_from is not None and known_at < self.known_from
        )


class RuleRegistry:
    """Immutable collection of rule versions. ``register``/``activate`` return new registries."""

    def __init__(self, versions: Iterable[RuleVersion] = ()) -> None:
        by_key: dict[str, RuleVersion] = {}
        for rv in versions:
            if rv.key in by_key:
                raise DomainValidationError(f"duplicate rule version {rv.key}")
            if rv.effective_from and rv.effective_to and rv.effective_to < rv.effective_from:
                raise DomainValidationError(f"{rv.key}: effective_to before effective_from")
            by_key[rv.key] = rv
        self._by_key = by_key

    def __iter__(self):
        return iter(sorted(self._by_key.values(), key=lambda r: (r.rule_id, r.version)))

    def __len__(self) -> int:
        return len(self._by_key)

    def register(self, rv: RuleVersion) -> RuleRegistry:
        return RuleRegistry([*self._by_key.values(), rv])

    def get(self, rule_id: str, version: str) -> RuleVersion:
        try:
            return self._by_key[f"{rule_id}@{version}"]
        except KeyError as e:
            raise RuleNotFoundError(f"{rule_id}@{version}") from e

    def versions(self, rule_id: str) -> list[RuleVersion]:
        return [rv for rv in self if rv.rule_id == rule_id]

    def resolve(self, rule_id: str, on: date, known_at: date | None = None) -> RuleVersion | None:
        """The active version applying on ``on`` (latest ``effective_from`` wins)."""
        cands = [rv for rv in self.versions(rule_id) if rv.applies_on(on, known_at)]
        if not cands:
            return None
        return max(cands, key=lambda r: (r.effective_from, r.version))

    def activate(self, rule_id: str, version: str, effective_from: date) -> RuleRegistry:
        """Return a registry where an inactive version gets an effective date."""
        rv = self.get(rule_id, version)
        new = replace(rv, effective_from=effective_from)
        return RuleRegistry([new if v.key == rv.key else v for v in self._by_key.values()])

    def fingerprint(self) -> str:
        return content_hash(
            [
                {
                    "key": rv.key,
                    "effective_from": rv.effective_from,
                    "effective_to": rv.effective_to,
                    "known_from": rv.known_from,
                    "params": dict(rv.params),
                }
                for rv in self
            ]
        )
