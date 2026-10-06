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


# .gitignore entries that are build output or editor state, not data that must never be
# published; every other entry must also be a scanner path rule
NOT_SENSITIVE = {
    "*.py[cod]",
    ".venv/",
    "*.egg-info/",
    "dist/",
    "build/",
    ".DS_Store",
    ".idea/",
    ".vscode/",
    "*.tsbuildinfo",
    "frontend/next-env.d.ts",
}


def _samples(pattern: str) -> list[str]:
    p = pattern.lstrip("/").replace("*", "x").replace("[cod]", "c")
    if p.endswith("/"):
        p += "f"
    out = [p]
    if "/" not in pattern.rstrip("/"):
        out.append("sub/" + p)  # an unanchored pattern also matches below the root
    return out


def test_every_sensitive_gitignore_entry_is_a_path_rule() -> None:
    scan = _load()
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    patterns = [x.strip() for x in lines if x.strip() and not x.startswith(("#", "!"))]
    assert "llm_cache/" in patterns and ".npmrc" in patterns
    missing = [
        sample
        for pat in patterns
        if pat not in NOT_SENSITIVE
        for sample in _samples(pat)
        if not list(scan.scan_path_name(sample))
    ]
    assert missing == []


def test_reviewed_forbidden_paths_are_found() -> None:
    scan = _load()
    for path in (
        "llm_cache/ab.json",
        "llm_replay/x.json",
        "llm_budget.jsonl",
        "blobs/ab/cdef",
        "data/results/ftc_eval.json",
        "docs/ip/sources/a.pdf",
        "x.sqlite-journal",
        "logs/app.txt",
        ".coverage",
        "htmlcov/index.html",
        ".claude/settings.local.json",
        "CLAUDE.local.md",
        ".npmrc",
        "frontend/.npmrc",
        ".pypirc",
        ".netrc",
        ".envrc",
    ):
        assert list(scan.scan_path_name(path)), path
    # tracked files that must stay publishable
    for path in ("docs/ip/README.md", "frontend/CLAUDE.md", ".env.prod.example", "docs/logs.md"):
        assert not list(scan.scan_path_name(path)), path


def test_a_comment_cannot_exempt_a_line() -> None:
    scan = _load()
    token = "ghp_" + secrets.token_hex(20)
    assert {f.rule for f in scan.scan_text("a.py", f'T = "{token}"\n')} == {"github-token"}
    marked = f'T = "{token}"  # secret-scan: allow\n'
    assert {f.rule for f in scan.scan_text("tests/a.py", marked)} == {"github-token"}
    # the scoped allowlist exempts its one rule, path and line shape only
    # the source text of tests/api/test_api_uploads.py: doubled backslashes inside a literal
    win = 'assert sanitize_filename("C:' + "\\\\Users\\\\x\\\\a.xlsx" + '") == "a.xlsx"'
    assert not list(scan.scan_text("tests/api/test_api_uploads.py", win))
    assert {f.rule for f in scan.scan_text("tests/api/other.py", win)} == {"personal-path"}
    leaked = win + f'  # T = "{token}"'
    assert {f.rule for f in scan.scan_text("tests/api/test_api_uploads.py", leaked)} == {
        "github-token"
    }
