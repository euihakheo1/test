"""Check that rule parameters in ``jettae.rules.kr_retail`` match the fetched source text.

Each check finds an evidence passage in the fetched paragraph texts (law manifest) with a
regular expression, extracts the number and compares it with the registry parameter.
Statuses: ``match`` / ``mismatch`` / ``not_found`` (pattern absent) / ``source_missing``
(document not fetched) / ``attention`` (an INACTIVE amendment's number appears in the
current text — its enforcement date should be reviewed) / ``info``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from jettae.rules.kr_retail import (
    RULE_CONSIGNMENT,
    RULE_DIRECT,
    RULE_INTEREST_RETAIL,
    RULE_INTEREST_SUBCONTRACT,
    RULE_SUBCONTRACT,
    builtin_registry,
)
from jettae.rules.registry import RuleRegistry, RuleVersion

PAT_DIRECT = re.compile(r"직매입[^.]*?상품수령일부터\s*(\d+)\s*일\s*이내")
PAT_CONSIGN = re.compile(r"(?:월\s*)?판매마감일부터\s*(\d+)\s*일\s*이내")
PAT_SUBCON = re.compile(r"수령일(?:\([^()]*\))?부터\s*(\d+)\s*일\s*이내")
PAT_RATE = re.compile(r"연리\s*(\d+(?:\.\d+)?)\s*%")
PAT_CAP = re.compile(r"연\s*100분의\s*(\d+)\s*이내")


@dataclass(frozen=True)
class CheckItem:
    rule: str  # rule_id@version
    param: str
    registry_value: str
    source_key: str
    found_value: str | None
    status: str
    evidence: str | None = None
    span: tuple[int, int] | None = None  # char span of the match inside ``evidence``
    source_url: str | None = None
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["span"] = list(self.span) if self.span else None
        return d


def _find(paragraphs: list[str], pat: re.Pattern[str]) -> tuple[str, re.Match[str]] | None:
    for p in paragraphs:
        m = pat.search(p)
        if m:
            return p, m
    return None


def _current(reg: RuleRegistry, rule_id: str) -> RuleVersion | None:
    active = [rv for rv in reg.versions(rule_id) if rv.effective_from is not None]
    return max(active, key=lambda r: (r.effective_from, r.version)) if active else None


def _pct(rate: Any) -> str:
    return str((Decimal(str(rate)) * 100).normalize())


def _num_check(
    rv: RuleVersion,
    param: str,
    expected: str,
    docs: dict[str, Any],
    key: str,
    pat: re.Pattern[str],
    note: str = "",
) -> CheckItem:
    d = docs.get(key) or {}
    if not d.get("ok"):
        return CheckItem(
            rv.key, param, expected, key, None, "source_missing", note=d.get("error", "not fetched")
        )
    hit = _find(d.get("paragraphs", []), pat)
    if hit is None:
        return CheckItem(
            rv.key, param, expected, key, None, "not_found", source_url=d.get("url"), note=note
        )
    para, m = hit
    found = m.group(1)
    status = "match" if Decimal(found) == Decimal(expected) else "mismatch"
    return CheckItem(
        rv.key, param, expected, key, found, status, para, m.span(1), d.get("url"), note
    )


def _effective_check(rv: RuleVersion, docs: dict[str, Any], key: str) -> CheckItem:
    d = docs.get(key) or {}
    expected = rv.effective_from.isoformat() if rv.effective_from else "None"
    if not d.get("ok"):
        return CheckItem(rv.key, "effective_from", expected, key, None, "source_missing")
    found = d.get("effective_date")
    number = d.get("number")
    status = "match" if found == expected else "mismatch"
    note = f"발령번호 {number}" + (
        "" if number == rv.version else f" (registry version {rv.version})"
    )
    if number != rv.version:
        status = "mismatch"
    return CheckItem(
        rv.key,
        "effective_from",
        expected,
        key,
        found,
        status,
        f"시행일자 {found}, 발령번호 {number}",
        None,
        d.get("url"),
        note,
    )


def run_checks(
    manifest: dict[str, Any] | None, registry: RuleRegistry | None = None
) -> list[CheckItem]:
    reg = registry or builtin_registry()
    docs: dict[str, Any] = (manifest or {}).get("documents", {})
    out: list[CheckItem] = []
    spec = [
        (RULE_DIRECT, "large_retail_art8", PAT_DIRECT),
        (RULE_CONSIGNMENT, "large_retail_art8", PAT_CONSIGN),
        (RULE_SUBCONTRACT, "subcontract_art13", PAT_SUBCON),
    ]
    for rule_id, key, pat in spec:
        rv = _current(reg, rule_id)
        if rv is None:
            continue
        out.append(_num_check(rv, "term_days", str(rv.params["term_days"]), docs, key, pat))
        # inactive versions (e.g. the 2026 amendment): their numbers must not be in force yet
        for iv in reg.versions(rule_id):
            if iv.effective_from is not None:
                continue
            c = _num_check(iv, "term_days", str(iv.params["term_days"]), docs, key, pat)
            if c.status == "match":
                out.append(
                    CheckItem(
                        iv.key,
                        "term_days",
                        c.registry_value,
                        key,
                        c.found_value,
                        "attention",
                        c.evidence,
                        c.span,
                        c.source_url,
                        "inactive version's term appears in the current text: "
                        "review enforcement date",
                    )
                )
            elif c.status == "mismatch":
                out.append(
                    CheckItem(
                        iv.key,
                        "term_days",
                        c.registry_value,
                        key,
                        c.found_value,
                        "info",
                        c.evidence,
                        c.span,
                        c.source_url,
                        "inactive version (effective_from=None); current text still states "
                        f"{c.found_value}일",
                    )
                )
    for rule_id, key, law_key, ref in (
        (RULE_INTEREST_RETAIL, "large_retail_interest", "large_retail_art8", "제8조"),
        (RULE_INTEREST_SUBCONTRACT, "subcontract_interest", "subcontract_art13", "제13조"),
    ):
        rv = _current(reg, rule_id)
        if rv is None:
            continue
        out.append(
            _num_check(rv, "annual_rate(%)", _pct(rv.params["annual_rate"]), docs, key, PAT_RATE)
        )
        out.append(_effective_check(rv, docs, key))
        d = docs.get(key) or {}
        if d.get("ok"):
            hit = _find(d.get("paragraphs", []), re.compile(re.escape(ref)))
            out.append(
                CheckItem(
                    rv.key,
                    "legal_basis",
                    ref,
                    key,
                    ref if hit else None,
                    "match" if hit else "not_found",
                    hit[0] if hit else None,
                    hit[1].span() if hit else None,
                    d.get("url"),
                    "notice cites the statute article it implements",
                )
            )
        if rule_id == RULE_INTEREST_RETAIL:
            law = docs.get(law_key) or {}
            hit = _find(law.get("paragraphs", []), PAT_CAP) if law.get("ok") else None
            if hit is not None:
                cap = Decimal(hit[1].group(1))
                rate = Decimal(_pct(rv.params["annual_rate"]))
                out.append(
                    CheckItem(
                        rv.key,
                        "annual_rate<=statutory cap(%)",
                        str(rate),
                        law_key,
                        str(cap),
                        "match" if rate <= cap else "mismatch",
                        hit[0],
                        hit[1].span(1),
                        law.get("url"),
                        "제8조 제3항: 연 100분의 40 이내에서 고시",
                    )
                )
    return out


def failed(
    items: list[CheckItem],
    required: tuple[str, ...] = ("large_retail_art8", "large_retail_interest"),
) -> list[CheckItem]:
    bad = []
    for c in items:
        missing = c.status in ("not_found", "source_missing") and c.source_key in required
        if c.status in ("mismatch", "attention") or missing:
            bad.append(c)
    return bad
