"""Second external review (2026-10-06): the live budget ledger does not depend on the working
directory (finding 7), and a torn last ledger line does not brick the budget (finding 10)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jettae.llm import (
    Budget,
    BudgetExceeded,
    BudgetStoreCorrupt,
    FileLockBudgetStore,
    LLMError,
    budget_from_env,
    default_ledger_path,
)


@pytest.mark.parametrize("var", ["LOCALAPPDATA", "XDG_STATE_HOME", "JETTAE_STATE_DIR"])
def test_two_runs_from_different_directories_share_one_limit(
    tmp_path: Path, monkeypatch: Any, var: str
) -> None:
    env = {"JETTAE_LLM_BUDGET_KRW": "100", var: str(tmp_path / "userdata")}
    (tmp_path / "cwdA").mkdir()
    (tmp_path / "cwdB").mkdir()
    monkeypatch.chdir(tmp_path / "cwdA")
    a = budget_from_env(env, live=True)
    r = a.reserve(Decimal(100), model="m", purpose="run A")
    a.settle(r, Decimal(100))
    monkeypatch.chdir(tmp_path / "cwdB")
    b = budget_from_env(env, live=True)
    assert isinstance(a.store, FileLockBudgetStore) and isinstance(b.store, FileLockBudgetStore)
    assert a.store.path == b.store.path and b.store.path.is_absolute()
    with pytest.raises(BudgetExceeded):
        b.reserve(Decimal(1), model="m", purpose="run B")
    assert not (tmp_path / "cwdA" / "var").exists() and not (tmp_path / "cwdB" / "var").exists()


def test_relative_ledger_paths_are_refused_in_live_mode(tmp_path: Path) -> None:
    with pytest.raises(LLMError, match="absolute"):
        budget_from_env(
            {"JETTAE_LLM_BUDGET_KRW": "10", "JETTAE_LLM_LEDGER": "var/x.jsonl"}, live=True
        )
    with pytest.raises(LLMError, match="absolute"):
        default_ledger_path({"JETTAE_STATE_DIR": "relative/dir"})
    # offline: no paid call is possible, a relative path is only a file location
    off = budget_from_env({"JETTAE_LLM_LEDGER": "var/x.jsonl"}, live=False)
    assert isinstance(off.store, FileLockBudgetStore)


def _reserve_line(rid: str) -> str:
    return json.dumps(
        {
            "event": "reserve",
            "at": "2026-10-06T00:00:00+00:00",
            "reservation": rid,
            "reserved_krw": "5",
            "expires_at": "2026-10-06T00:15:00+00:00",
        }
    )


def test_torn_last_line_is_dropped_and_the_budget_keeps_working(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    b = Budget(Decimal(100), ledger_path=path)
    first = b.reserve(Decimal(10), model="m", purpose="x")
    # a process killed while appending: an unterminated reserve line
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"event": "reserve", "reservation": "dead", "reser')
    # every operation still works; the unfinished line never counted (no call was made)
    assert b.remaining_krw == Decimal(90)
    b.settle(first, Decimal(7))
    second = Budget(Decimal(100), ledger_path=path).reserve(Decimal(50), model="m", purpose="y")
    assert second is not None
    text = path.read_text(encoding="utf-8")
    assert "reser\n" not in text and '"dead"' not in text and text.endswith("\n")
    assert all(json.loads(line) for line in text.splitlines())


def test_unparseable_last_line_with_newline_is_also_unfinished(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(_reserve_line("r1") + "\n" + '{"event": "settle", "reserv\n', encoding="utf-8")
    store = FileLockBudgetStore(path)
    b = Budget(Decimal(100), store=store)
    # r1 stays open (its settle line never finished): it keeps counting at 5
    assert b.remaining_krw == Decimal(95)
    assert path.read_text(encoding="utf-8") == _reserve_line("r1") + "\n"


def test_damage_before_the_last_line_is_a_dedicated_llm_error(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text("not json\n" + _reserve_line("r1") + "\n", encoding="utf-8")
    b = Budget(Decimal(100), ledger_path=path)
    with pytest.raises(BudgetStoreCorrupt) as e:
        b.reserve(Decimal(1), model="m", purpose="x")
    assert isinstance(e.value, LLMError)
    assert path.read_text(encoding="utf-8").startswith("not json\n")  # never rewritten
