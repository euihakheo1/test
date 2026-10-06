"""Mechanical verification checks (pure). Passing them means VERIFIED, not legal correctness.

- citation: the span excerpt exists in the source text (whitespace-normalised);
- numbers: every number / date in an explanation equals a number / date the engine produced;
- conservation: allocations never over-use a payment or an item;
- wording: user-facing text contains no legal-conclusion phrases (AGENTS.md rule 8).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from jettae.domain.models import Allocation, Computation, Decision, SourceSpan
from jettae.domain.money import Money
from jettae.recon.allocation import check_conservation


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    details: tuple[str, ...] = ()


_WS = re.compile(r"\s+")
_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_IDENT = re.compile(r"\[[^\]\n]*\]")
_NUM = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])-?\d+(?:\.\d+)?")
FORBIDDEN_PHRASES = ("위법", "불법", "받을 수 있", "받을 수있", "청구할 수 있", "승소")


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


def check_citation(span: SourceSpan, source_text: str | None) -> CheckResult:
    if source_text is None:
        return CheckResult("citation", False, (f"{span.doc_version_id}: 원문 텍스트 없음",))
    if not span.excerpt.strip():
        return CheckResult("citation", False, (f"{span.doc_version_id}: 발췌문이 비어 있음",))
    ok = _norm(span.excerpt) in _norm(source_text)
    return CheckResult(
        "citation",
        ok,
        () if ok else (f"{span.doc_version_id}: 발췌문이 원문에 없음: {span.excerpt[:40]!r}",),
    )


def extract_numbers(text: str) -> tuple[list[Decimal], list[date]]:
    """Numbers and ISO dates in ``text``. Identifiers written in ``[brackets]`` are skipped."""
    text = _IDENT.sub(" ", text)
    dates: list[date] = []
    for m in _DATE.finditer(text):
        try:
            dates.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    stripped = _DATE.sub(" ", text)
    nums = [Decimal(m.group(0).replace(",", "")) for m in _NUM.finditer(stripped)]
    return nums, dates


def collect_engine_values(obj: Any, nums: set[Decimal], dates: set[date]) -> None:
    """Collect all numbers and dates appearing anywhere in engine output."""
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, Money):
        nums.add(Decimal(obj.amount))
    elif isinstance(obj, int | Decimal):
        nums.add(Decimal(obj))
    elif isinstance(obj, date):
        dates.add(obj)
    elif isinstance(obj, str):
        n, d = extract_numbers(obj)
        nums.update(n)
        dates.update(d)
    elif isinstance(obj, Mapping):
        for v in obj.values():
            collect_engine_values(v, nums, dates)
    elif isinstance(obj, list | tuple | set | frozenset):
        for v in obj:
            collect_engine_values(v, nums, dates)
    elif isinstance(obj, Computation | Allocation):
        collect_engine_values(obj.__dict__, nums, dates)


def decision_values(decision: Decision) -> tuple[set[Decimal], set[date]]:
    nums: set[Decimal] = set()
    dates: set[date] = set()
    for c in decision.computations:
        collect_engine_values(c.inputs, nums, dates)
        collect_engine_values(c.outputs, nums, dates)
    for a in decision.allocations:
        collect_engine_values(a.amount, nums, dates)
    collect_engine_values(decision.assumptions, nums, dates)
    return nums, dates


def check_numbers(
    text: str,
    allowed_numbers: Iterable[int | Decimal],
    allowed_dates: Iterable[date] = (),
    *,
    ignore_small: int = 0,
) -> CheckResult:
    """Every number/date in ``text`` must be an engine value. Numbers with absolute value
    <= ``ignore_small`` (e.g. list ordinals) are ignored when > 0."""
    nums, dates = extract_numbers(text)
    allowed_n = {Decimal(x) for x in allowed_numbers}
    allowed_d = set(allowed_dates)
    bad = [
        str(n) for n in nums if n not in allowed_n and not (ignore_small and abs(n) <= ignore_small)
    ]
    bad += [d.isoformat() for d in dates if d not in allowed_d]
    return CheckResult("numbers", not bad, tuple(f"엔진 계산에 없는 값: {b}" for b in bad))


def check_explanation(text: str, decision: Decision) -> CheckResult:
    nums, dates = decision_values(decision)
    return check_numbers(text, nums, dates)


def check_allocations(
    allocations: Iterable[Allocation],
    payments: Mapping[str, Money],
    items: Mapping[str, Money],
) -> CheckResult:
    v = check_conservation(allocations, payments, items)
    return CheckResult("conservation", not v, tuple(v))


def check_wording(text: str) -> CheckResult:
    hits = [p for p in FORBIDDEN_PHRASES if p in text]
    return CheckResult("wording", not hits, tuple(f"판단 표현 사용: {h}" for h in hits))


def verify_decision(
    decision: Decision,
    *,
    explanation: str | None = None,
    sources: Mapping[str, str] | None = None,
    spans: Iterable[SourceSpan] = (),
    payments: Mapping[str, Money] | None = None,
    items: Mapping[str, Money] | None = None,
) -> list[CheckResult]:
    """Run all applicable checks for one decision."""
    results: list[CheckResult] = []
    if explanation is not None:
        results.append(check_explanation(explanation, decision))
        results.append(check_wording(explanation))
    for span in spans:
        results.append(check_citation(span, (sources or {}).get(span.doc_version_id)))
    if payments is not None and items is not None:
        results.append(check_allocations(decision.allocations, payments, items))
    return results
