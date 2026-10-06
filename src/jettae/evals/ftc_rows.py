"""FTC evaluation, per row: recompute one transcribed row with the engine (E2/E3).

Each row yields a :class:`RowResult` with one :class:`VariantCheck` per computation
variant. A check is ``True``/``False`` when both the engine value and the table value exist,
and ``None`` when it cannot be evaluated (the table has no such column value, or the engine
did not compute it). ``None`` is "not evaluated" -- never "passed".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from jettae.domain.dates import days_between
from jettae.domain.money import Money, RoundingMode
from jettae.domain.status import TradeType
from jettae.evals.ftc_inputs import (
    VARIANTS,
    SeedRow,
    TableMeta,
    is_uncertain,
    parse_date_cell,
    parse_int_cell,
    parse_range,
    variant_label,
)
from jettae.rules.kr_retail import (
    INTEREST_RULE,
    DueResult,
    Insufficient,
    builtin_registry,
    compute_due,
    compute_interest,
)


@dataclass
class VariantCheck:
    label: str
    due: date | None
    delay_days: int | None
    interest: int | None
    due_ok: bool | None
    delay_ok: bool | None
    interest_ok: bool | None

    @property
    def checks(self) -> tuple[bool | None, bool | None, bool | None]:
        return (self.due_ok, self.delay_ok, self.interest_ok)

    @property
    def evaluated(self) -> int:
        """How many of the three checks could be scored (engine and table value present)."""
        return sum(1 for c in self.checks if c is not None)

    @property
    def fully_evaluated(self) -> bool:
        return self.evaluated == 3

    @property
    def all_ok(self) -> bool:
        """All three checks were evaluated AND all match. A ``None`` (not evaluated) check
        fails this metric: "all three" never means "all of the ones we could check"."""
        return self.fully_evaluated and all(self.checks)

    @property
    def all_available_ok(self) -> bool:
        """Every evaluable check matches (at least one evaluable). Weaker than ``all_ok``."""
        avail = [c for c in self.checks if c is not None]
        return bool(avail) and all(avail)

    @property
    def fully_computed(self) -> bool:
        """The engine produced a due date, delay days and interest (table values aside)."""
        return self.due is not None and self.delay_days is not None and self.interest is not None


@dataclass
class RowResult:
    key: str
    decision_id: str
    flseq: str
    table_label: str
    mode: str  # "e2" | "e2_range" | "e2_cond" | "skip"
    uncertain: bool
    abstained: bool | None = None  # E3: engine returned Insufficient
    abstain_missing: list[str] = field(default_factory=list)  # Insufficient.missing
    rule_missing: bool = False  # engine has no rule version for the base date
    required_documents: list[str] = field(default_factory=list)
    variants: list[VariantCheck] = field(default_factory=list)
    interest_engine_missing: bool = False
    arith: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def matched(self, what: str) -> list[str]:
        return [v.label for v in self.variants if getattr(v, what)]

    @property
    def evaluable_checks(self) -> int:
        """Checks this row can be scored on (max over variants; 0 when the engine abstained)."""
        return max((v.evaluated for v in self.variants), default=0)

    @property
    def fully_computed(self) -> bool:
        """The engine computed all three values in every variant (False if no variant)."""
        return bool(self.variants) and all(v.fully_computed for v in self.variants)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "decision_id": self.decision_id,
            "flseq": self.flseq,
            "table_label": self.table_label,
            "mode": self.mode,
            "uncertain_transcription": self.uncertain,
            "abstained": self.abstained,
            "abstain_missing": self.abstain_missing,
            "rule_missing": self.rule_missing,
            "required_documents": self.required_documents,
            "interest_engine_missing": self.interest_engine_missing,
            "variants": [
                {
                    "label": v.label,
                    "due": v.due.isoformat() if v.due else None,
                    "delay_days": v.delay_days,
                    "interest_krw": v.interest,
                    "due_ok": v.due_ok,
                    "delay_ok": v.delay_ok,
                    "interest_ok": v.interest_ok,
                    "evaluated_checks": v.evaluated,
                }
                for v in self.variants
            ],
            "evaluable_checks": self.evaluable_checks,
            "fully_computed": self.fully_computed,
            "matched_all_three": self.matched("all_ok"),
            "matched_all_available": self.matched("all_available_ok"),
            "arith": self.arith,
            "note": self.note,
        }


def _interest_ok(engine: int | None, table: int | None, mult: int) -> bool | None:
    if engine is None or table is None:
        return None
    return abs(engine - table * mult) <= (1 if mult == 1 else mult)


def _table_due_as_due(meta: TableMeta | None, value: date) -> date:
    """Convert the table's due column to a due date (``delay_start`` = due date + 1 day)."""
    if meta is not None and meta["due_col_semantics"] == "delay_start":
        return value - timedelta(days=1)
    return value


def evaluate_row(
    row: SeedRow, meta: TableMeta | None, rate: dict[str, Any] | None = None
) -> RowResult:
    res = RowResult(
        key=row.key,
        decision_id=row["decision_id"],
        flseq=row["flseq"],
        table_label=row["table_label"],
        mode="skip",
        uncertain=is_uncertain(row["notes"]),
    )
    tt = row.trade_type
    if tt is None:
        res.note = "deal_type missing"
        return res
    mult = row.multiplier
    if row.is_range:
        return _evaluate_range(row, meta, res, tt)

    base = parse_date_cell(row["base_date"])
    paid = parse_date_cell(row["paid_date"])
    principal = parse_int_cell(row["principal_krw"])
    t_delay = parse_int_cell(row["delay_days_in_table"])
    t_interest = parse_int_cell(row["interest_in_table"])
    t_due_raw = parse_date_cell(row["due_date_in_table"])
    money = Money((principal or 0) * mult)

    # E3: the engine is called exactly as an application would call it
    probe = compute_due(tt, base, money, paid_date=paid)
    res.abstained = isinstance(probe, Insufficient)
    if isinstance(probe, Insufficient):
        res.required_documents = list(probe.required_documents)
        res.abstain_missing = list(probe.missing)
        res.rule_missing = "rule_version" in probe.missing

    if base is None:
        res.mode = "e2_cond" if (t_due_raw and paid) else "skip"
        if t_due_raw and paid:
            due = _table_due_as_due(meta, t_due_raw)
            delay = max(0, days_between(due, paid))
            reg = builtin_registry()
            irv = reg.resolve(INTEREST_RULE[tt], due + timedelta(days=1))
            for rounding in (RoundingMode.FLOOR, RoundingMode.HALF_UP):
                interest = None
                if irv is not None and principal is not None:
                    interest = compute_interest(
                        money,
                        Decimal(irv.params["annual_rate"]),
                        delay,
                        int(irv.params["day_count"]),
                        rounding,
                    ).amount
                res.variants.append(
                    VariantCheck(
                        label=f"table_due/{rounding.value}",
                        due=due,
                        delay_days=delay,
                        interest=interest,
                        due_ok=None,
                        delay_ok=(delay == t_delay) if t_delay is not None else None,
                        interest_ok=_interest_ok(interest, t_interest, mult),
                    )
                )
            res.interest_engine_missing = irv is None
            if irv is None:
                res.arith = _arith(money, delay, t_interest, mult, rate)
        return res

    res.mode = "e2"
    for rollover, rounding in VARIANTS:
        r = compute_due(tt, base, money, paid_date=paid, rollover=rollover, rounding=rounding)
        if not isinstance(r, DueResult):
            res.note = "engine abstained although a base date is present"
            continue
        v = r.variants[0]
        due_ok = None
        if t_due_raw is not None:
            due_ok = _table_due_as_due(meta, t_due_raw) == v.due_date
        interest = v.interest.amount if v.interest is not None else None
        if v.interest is None:
            res.interest_engine_missing = True
        res.variants.append(
            VariantCheck(
                label=variant_label(rollover, rounding),
                due=v.due_date,
                delay_days=v.delay_days if paid else None,
                interest=interest if principal is not None and paid else None,
                due_ok=due_ok,
                delay_ok=(v.delay_days == t_delay) if (t_delay is not None and paid) else None,
                interest_ok=_interest_ok(
                    interest if principal is not None and paid else None, t_interest, mult
                ),
            )
        )
    if res.interest_engine_missing and paid and principal is not None:
        res.arith = {
            v.label: _arith(money, v.delay_days or 0, t_interest, mult, rate, v.label)
            for v in res.variants
        }
    return res


def _arith(
    money: Money,
    delay: int,
    t_interest: int | None,
    mult: int,
    rate: dict[str, Any] | None,
    label: str = "",
) -> dict[str, Any]:
    """Interest with the rate *stated in the decision text* (engine registry had no version)."""
    if rate is None or t_interest is None:
        return {"computed": False, "reason": "no stated rate or no table interest"}
    pct = Decimal(rate["percent"]) / Decimal(100)
    out: dict[str, Any] = {"computed": True, "rate_percent": rate["percent"]}
    for rounding in (RoundingMode.FLOOR, RoundingMode.HALF_UP):
        val = compute_interest(money, pct, delay, 365, rounding).amount
        out[rounding.value] = {"interest_krw": val, "ok": _interest_ok(val, t_interest, mult)}
    return out


def _evaluate_range(
    row: SeedRow, meta: TableMeta | None, res: RowResult, tt: TradeType
) -> RowResult:
    res.mode = "e2_range"
    b = parse_range(row["base_date"])
    d = parse_range(row["due_date_in_table"])
    if b is None or d is None:
        res.mode = "skip"
        res.note = "range row without both base and due ranges"
        return res
    bases = [date.fromisoformat(x) for x in b]
    dues = [_table_due_as_due(meta, date.fromisoformat(x)) for x in d]
    probes = [compute_due(tt, bd, Money(0)) for bd in bases]
    res.abstained = any(isinstance(p, Insufficient) for p in probes)
    res.abstain_missing = sorted(
        {m for p in probes if isinstance(p, Insufficient) for m in p.missing}
    )
    res.rule_missing = "rule_version" in res.abstain_missing
    if res.rule_missing:
        res.note = (
            f"range {b[0]}..{b[1]}: no registered {tt.value} rule version for these base dates"
        )
        return res
    for rollover in (False, True):
        got = []
        for bd in bases:
            r = compute_due(tt, bd, Money(0), rollover=rollover)
            got.append(r.variants[0].due_date if isinstance(r, DueResult) else None)
        res.variants.append(
            VariantCheck(
                label=f"rollover_{'on' if rollover else 'off'}",
                due=got[0],
                delay_days=None,
                interest=None,
                due_ok=got == dues,
                delay_ok=None,
                interest_ok=None,
            )
        )
    res.note = f"range endpoints {b[0]}..{b[1]} -> engine dues " + ", ".join(
        f"{v.label}={[g.isoformat() if g else None for g in [v.due]]}" for v in res.variants
    )
    return res
