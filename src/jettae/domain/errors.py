"""Domain error hierarchy (pure)."""

from __future__ import annotations


class JettaeError(Exception):
    """Base class for all domain/application errors."""


class InvalidMoneyError(JettaeError, ValueError):
    """Money constructed with a non-integer amount or an invalid currency code."""


class CurrencyMismatchError(JettaeError, ValueError):
    """Arithmetic or comparison between different currencies."""


class DomainValidationError(JettaeError, ValueError):
    """A domain invariant was violated."""


class ConservationError(DomainValidationError):
    """Allocation conservation violated (double use or over-allocation)."""


class RuleNotFoundError(JettaeError, LookupError):
    """No rule version registered under the given id/version."""


class NotFoundError(JettaeError, LookupError):
    """Entity not found (within the tenant)."""


class TenantMismatchError(JettaeError, PermissionError):
    """An object of another tenant was referenced."""


class StaleResultError(JettaeError):
    """Optimistic concurrency failure: expected result hash is not the current one."""

    def __init__(self, decision_id: str, expected: str, current: str | None) -> None:
        super().__init__(
            f"decision {decision_id}: expected result hash {expected[:12]}..., "
            f"current is {(current or 'none')[:12]}..."
        )
        self.decision_id = decision_id
        self.expected = expected
        self.current = current


class ReportValidityError(JettaeError):
    """A report was requested for decisions whose current validity is not approved."""

    def __init__(self, invalid: dict[str, str]) -> None:
        super().__init__(f"decisions not currently approved: {sorted(invalid)}")
        self.invalid = invalid
