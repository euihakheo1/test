"""scripts/secret_scan.py: finds secrets in the publish set and in deleted history, and never
prints the value it found.

Secret-looking values are generated at run time, so this file itself stays clean for the scan.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from jettae.config import REPO_ROOT

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "secret_scan", REPO_ROOT / "scripts" / "secret_scan.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


def test_rules_hit_real_looking_values_and_skip_placeholders() -> None:
    scan = _load()
    value = secrets.token_urlsafe(24)
    rules = {f.rule for f in scan.scan_text("a.env", f"JETTAE_JWT_SECRET={value}\n")}
    assert rules == {"secret-assignment"}
    assert not list(scan.scan_text("a", "JETTAE_JWT_SECRET=change-me-generate-a-secret\n"))
    url = "postgresql+psycopg://app:" + value + "@db:5432/x"
    assert {f.rule for f in scan.scan_text("a", url)} == {"url-credential"}
    home = "C:" + "/Users/" + "someone" + "/project"
    assert {f.rule for f in scan.scan_text("a", home)} == {"personal-path"}
    assert {f.rule for f in scan.scan_path_name(".env")} == {"env-file"}
    assert not list(scan.scan_path_name(".env.prod.example"))
    assert {f.rule for f in scan.scan_path_name("var/jettae.db")} >= {"database-file"}


def test_history_finds_a_deleted_secret_without_printing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scan = _load()
    value = "sk-ant-" + secrets.token_urlsafe(30)
    _git(tmp_path, "init", "-q")
    (tmp_path / "config.py").write_text(f'KEY = "{value}"\n', encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "add")
    (tmp_path / "config.py").write_text("KEY = None\n", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "remove")

    assert scan.main(["--root", str(tmp_path), "--no-history"]) == 0  # current files are clean
    capsys.readouterr()
    assert scan.main(["--root", str(tmp_path), "--json"]) == 1
    out = capsys.readouterr().out
    assert value not in out
    report = json.loads(out)
    assert report["summary"]["history_commits"] == 2
    assert {f["rule"] for f in report["findings"]} == {"anthropic-api-key"}
    assert all(f["path"] == "config.py" and f["blob"] for f in report["findings"])
