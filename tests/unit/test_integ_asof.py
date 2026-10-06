"""Integrator regressions: reference date (as_of) handling, AMBIGUOUS delay withholding,
assumption text (hand-written fixtures only)."""

import dataclasses
from datetime import UTC, date, datetime

from jt_unit_helpers import T, fact, inv, snapshot, txn

from jettae.app import JettaeService, in_memory_repositories
from jettae.app.explain import render_explanation
from jettae.domain import Change, ChangeKind, DocKind, ReconcileStatus, ReviewStatus
from jettae.evidence import full_recompute, incremental_recompute
from jettae.evidence.snapshot import AFTER_AS_OF_REASON


class Clock:
    def __init__(self, d: date):
        self.now = datetime(d.year, d.month, d.day, 3, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def set(self, d: date) -> None:
        self.now = datetime(d.year, d.month, d.day, 3, 0, tzinfo=UTC)


def _service(clock: Clock) -> JettaeService:
    s = JettaeService(in_memory_repositories(), clock=clock)
    s.register_document(
        T, filename="a.csv", content=b"x", media_type="text/csv", kind=DocKind.BANK, text="x"
    )
    return s


def _record(s: JettaeService, records) -> list[Change]:
    facts = [fact(fid, r.id, r.amount.amount) for r in records for fid in r.facts]
    return s.record_facts(T, facts=facts, records=records)


def _due(dec):
    return dec.computation("due")


def test_paid_on_time_approval_survives_a_later_rerun():
    clock = Clock(date(2025, 11, 1))
    s = _service(clock)
    _record(s, [inv("I1", 1_000_000), txn("T1", 1_000_000, date(2025, 9, 1))])
    s.run_analysis(T)
    d = s.repos.decisions.get_current(T, "dec:I1")
    assert d.status is ReconcileStatus.MATCHED
    assert "as_of" not in _due(d).inputs  # no open tranche: result does not depend on as_of
    s.approve(T, "dec:I1", d.result_hash, "kim")
    clock.set(date(2025, 11, 2))
    s.run_analysis(T)
    assert s.decision_view(T, "dec:I1").review_status is ReviewStatus.APPROVED


def test_unpaid_decision_depends_on_as_of_and_apply_change_rebases_it():
    clock = Clock(date(2025, 11, 2))
    s = _service(clock)
    _record(s, [inv("I1", 1_000_000), txn("T1", 1_000_000, date(2025, 9, 1))])
    s.run_analysis(T)
    clock.set(date(2026, 1, 31))
    i2 = inv("I2", 500_000, cp="다라상사", received=date(2025, 8, 1))
    changes = _record(s, [i2])
    out = s.apply_change(T, changes, verify_full=True)
    assert out.equivalent_to_full is True
    d2 = s.repos.decisions.get_current(T, "dec:I2")
    due = _due(d2)
    assert due.inputs["as_of"] == date(2026, 1, 31)
    # 2025-08-01 + 60 days = 2025-09-30 -> unpaid until 2026-01-31 = 123 days
    assert max(v["max_delay_days"] for v in due.outputs["variants"]) == 123
    assert "미지급분 계산 기준일(as_of): 2026-01-31" in render_explanation(d2)
    # the fully paid decision (other counterparty) is unchanged by the new reference date
    assert "dec:I1" not in out.changed


def test_explicit_as_of_is_kept_by_apply_change():
    clock = Clock(date(2026, 1, 31))
    s = _service(clock)
    _record(s, [inv("I1", 1_000_000)])
    s.run_analysis(T, as_of=date(2025, 11, 1))
    changes = _record(s, [inv("I2", 10, received=date(2025, 8, 1))])
    s.apply_change(T, changes, as_of=date(2025, 11, 1))
    assert _due(s.repos.decisions.get_current(T, "dec:I1")).inputs["as_of"] == date(2025, 11, 1)


def test_assumptions_name_tranche_end_dates_not_unset_as_of():
    snap = snapshot([inv("I1", 1_000_000)], [txn("T1", 400_000, date(2025, 10, 20))])
    dec = full_recompute(snap).decisions["dec:I1"]
    text = render_explanation(dec)
    assert "미지정: 지연일수 0" not in text
    assert "미지급 600,000원: 기준일(as_of) 2025-11-01까지" in text
    assert "입금 [T1] 400,000원: 입금일 2025-10-20까지" in text


def test_ambiguous_items_get_no_delay_or_interest():
    snap = snapshot(
        [inv("I1", 500_000), inv("I2", 500_000, received=date(2025, 8, 8))],
        [txn("T1", 500_000, date(2025, 9, 1))],
    )
    res = full_recompute(snap)
    for did in ("dec:I1", "dec:I2"):
        dec = res.decisions[did]
        assert dec.status is ReconcileStatus.AMBIGUOUS
        out = _due(dec).outputs
        assert out["delay_withheld"] == "allocation"
        for v in out["variants"]:
            assert v["due_date"] is not None
            assert v["max_delay_days"] is None and v["interest_total"] is None
            assert v["tranches"] == []
        assert "allocation" in dec.unresolved
        assert "미계산(입금 배분 미확정)" in render_explanation(dec)


def test_payments_after_as_of_are_excluded():
    snap = snapshot(
        [inv("I1", 1_000_000)], [txn("T1", 1_000_000, date(2025, 12, 31))], as_of=date(2025, 11, 1)
    )
    res = full_recompute(snap)
    dec = res.decisions["dec:I1"]
    assert dec.status is ReconcileStatus.UNMATCHED
    variants = _due(dec).outputs["variants"]
    # due 2025-10-06, unpaid as of 2025-11-01 -> 26 days (not 86 days to 2025-12-31)
    assert max(v["max_delay_days"] for v in variants) == 26
    assert ("T1", AFTER_AS_OF_REASON) in res.unattributed
    # moving as_of past the payment brings it in; incremental equals full
    later = dataclasses.replace(
        snap, config=dataclasses.replace(snap.config, as_of=date(2026, 1, 5))
    )
    inc, plan, _ = incremental_recompute(
        res, later, [Change(ChangeKind.UPDATE, "config", "cfg", later.config)]
    )
    assert not plan.fallback_full
    assert inc.comparable() == full_recompute(later).comparable()
    assert inc.decisions["dec:I1"].status is ReconcileStatus.MATCHED
