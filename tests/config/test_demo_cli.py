"""``jettae demo run`` and the root CLI startup check.

The demo reads the repository's real transcription (data/seeds/ftc_rows.csv); these tests check
that it copies values instead of inventing them, runs only in dev, and never writes the
printed password to disk."""

from __future__ import annotations

import csv
import io
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jettae.cli import app
from jettae.demo import DEFAULT_ROWS, SOURCE_LABEL, build_files


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    saved = dict(os.environ)
    for k in list(os.environ):
        if k.upper().startswith(("JETTAE_", "ANTHROPIC_", "OPENAI_")):
            del os.environ[k]
    monkeypatch.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _rows(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def test_demo_files_copy_seed_values_without_inventing_any():
    files = build_files()
    seed = list(csv.DictReader(DEFAULT_ROWS.open(encoding="utf-8-sig")))
    settle, bank = _rows(files.settlement_csv), _rows(files.bank_csv)
    assert len(settle) == len(bank) == files.rows_used
    assert files.rows_used + sum(files.skipped.values()) == len(seed)
    by_ref = {}
    for r in seed:
        table = re.sub(r"\s+", "", r["table_label"])
        by_ref[f"{r['case_no']}-{table}-{int(r['row_idx']):02d}"] = r
    for s, b in zip(settle, bank, strict=True):
        src = by_ref[s["정산번호"]]
        assert s["정산금액"] == src["principal_krw"] == b["입금액"]
        assert b["거래일자"] == src["paid_date"]
        printed = src["base_date"]
        # a base date appears only when the table printed one, in exactly one column
        assert {s["상품수령일"], s["판매마감일"]} - {""} == ({printed} if printed else set())
    assert files.rows_without_base_date > 0  # tables without base-date column stay withheld


def test_demo_refuses_non_dev_environment(env):
    os.environ["JETTAE_ENV"] = "test"
    os.environ["JETTAE_DATABASE_URL"] = f"sqlite:///{(env / 'x.db').as_posix()}"
    res = CliRunner().invoke(app, ["demo", "run"])
    assert res.exit_code != 0
    assert "JETTAE_ENV=dev" in (res.output + str(res.exception))
    assert not (env / "x.db").exists()


def test_demo_runs_end_to_end_and_never_stores_the_password(env):
    os.environ.update(
        {
            "JETTAE_ENV": "dev",
            "JETTAE_DATABASE_URL": f"sqlite:///{(env / 'var' / 'd.db').as_posix()}",
            "JETTAE_BLOB_DIR": str(env / "var" / "blobs"),
            "JETTAE_ARGON2_TIME_COST": "1",
            "JETTAE_ARGON2_MEMORY_KIB": "8192",
            "JETTAE_ARGON2_PARALLELISM": "1",
        }
    )
    res = CliRunner().invoke(app, ["demo", "run", "--out-dir", str(env / "out")])
    assert res.exit_code == 0, res.output
    assert SOURCE_LABEL in res.output
    assert "MATCHED" in res.output and "INSUFFICIENT_EVIDENCE" in res.output
    m = re.search(r"비밀번호 (\S+)", res.output)
    assert m is not None
    password = m.group(1).encode()
    for p in env.rglob("*"):
        if p.is_file():
            assert password not in p.read_bytes(), p


@pytest.mark.parametrize("cmd", [["rules", "list"], ["demo", "run"], ["sources", "law", "--help"]])
def test_root_cli_rejects_unknown_environment_name(env, cmd):
    os.environ["JETTAE_ENV"] = "production"
    res = CliRunner().invoke(app, cmd)
    assert res.exit_code != 0
    assert "JETTAE_ENV must be one of" in (res.output + str(res.exception))


def test_root_cli_loads_dotenv_from_working_directory(env):
    (env / ".env").write_text("JETTAE_ENV=staging\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["rules", "list"])
    assert res.exit_code != 0
    assert "'staging'" in (res.output + str(res.exception))


def test_bpi_source_command_does_not_need_drf_key_in_prod(env):
    from jettae.config import Settings, environment_problems

    st = Settings(env="prod")
    assert environment_problems(st, "sources:bpi2019", {}) == []
    assert any("JETTAE_DRF_OC" in p for p in environment_problems(st, "sources:ftc", {}))
    assert any("JETTAE_DRF_OC" in p for p in environment_problems(st, "sources:law", {}))
