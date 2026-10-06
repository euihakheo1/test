"""Secret and private-data scan for the files Git would publish and for the whole history.

Usage (repository root, stdlib only, no network):

    python scripts/secret_scan.py            # files git tracks or would add + every commit
    python scripts/secret_scan.py --no-history
    python scripts/secret_scan.py --json     # machine-readable findings

What is scanned:
- *publish set*: ``git ls-files`` plus ``git ls-files --others --exclude-standard``, i.e. the
  tracked files and everything ``git add -A`` would add. Ignored files (``.env``, ``var/``,
  ``data/raw/``, caches, ``node_modules``) are not part of it; the scan reports when one of
  them is tracked anyway.
- *history*: every blob reachable from any ref (``git rev-list --all --objects``), so a
  secret that was committed and later deleted is still found.

Output never contains the matched value: only rule id, path, line and (for history) the blob
id. Exit code 1 when anything is found, 0 otherwise, 2 when git is unavailable.

The rules are high-confidence patterns (key formats with fixed prefixes, private-key blocks,
credentials inside URLs, assignments of secret-named variables to non-placeholder values) and
path rules for files that must never be published (databases, env files, logs, caches, raw
downloads). They do not replace a full scanner such as gitleaks; they make the publication
rules of this repository executable in CI without extra tools.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

# Values that are obviously not real secrets (examples, tests, documentation placeholders).
PLACEHOLDER = re.compile(
    r"(change[-_]?me|replace[-_]?me|placeholder|example|dummy|not[-_]a[-_]secret|"
    r"your[-_]|<[^>]*>|\$\{?[A-Z_]+\}?|x{6,}|\*{3,}|\.\.\.|test[-_]?secret|fake|redacted|"
    r"insecure|sample|\btest\b|e2e|should[-_]not|current-password)",
    re.IGNORECASE,
)

CONTENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY"),
    ),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("anthropic-api-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    # (?!ant-): an Anthropic key would otherwise be reported twice
    ("openai-api-key", re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("stripe-live-key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("jettae-api-token", re.compile(r"\bjtk_[A-Za-z0-9_\-]{24,}")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ),
)

# NAME=value / "name": "value" / name = "value" with a secret-like name.
ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?P<name>[A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY|
        _TOKEN|DRF_OC)[A-Z0-9_]*)\b
    ["']?\s*[:=]\s*["']?(?P<value>[^\s"'#,;(){}\[\]]{8,})(?P<call>\()?
    """
)
URL_CREDENTIAL = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@\"']+:(?P<pw>[^\s/@\"']{3,})@")

# Paths that must never be published, whatever their content. They mirror every runtime,
# user-data, secret and cache entry of .gitignore (tests/config/test_secret_scan.py checks
# that each such .gitignore entry yields a finding), so `git add -f` or a later .gitignore
# edit cannot publish one of them silently.
PATH_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("env-file", re.compile(r"(^|/)\.env(\.[^/]*)?$")),
    (
        "database-file",
        re.compile(r"\.(db|sqlite|sqlite3|db-journal|db-wal|db-shm|sqlite-journal|dump)$"),
    ),
    ("log-file", re.compile(r"\.log$|(^|/)logs/")),
    ("key-file", re.compile(r"\.(pem|key|p12|pfx)$")),
    # local runtime state: uploads (blob store), DB, demo files
    ("runtime-dir", re.compile(r"(^|/)(var|blobs)/")),
    # LLM replay cache (holds company data sent to a model) and the budget ledger
    ("llm-cache", re.compile(r"(^|/)(llm_cache|llm_replay)/|(^|/)llm_budget\.jsonl$")),
    ("raw-download", re.compile(r"^data/raw/|^docs/ip/sources/[^/]*\.pdf$")),
    ("eval-results", re.compile(r"^data/results/")),
    (
        "cache-dir",
        re.compile(
            r"(^|/)(\.mypy_cache|\.pytest_cache|\.ruff_cache|\.hypothesis|"
            r"__pycache__|node_modules|\.next|\.e2e-tmp|test-results|"
            r"playwright-report|htmlcov|coverage)/"
        ),
    ),
    ("coverage-data", re.compile(r"(^|/)\.coverage(\.[^/]*)?$|(^|/)coverage\.xml$")),
    # per-user tool settings that can hold credentials or machine-specific paths
    (
        "local-config",
        re.compile(
            r"(^|/)(\.npmrc|\.pypirc|\.netrc|\.envrc|CLAUDE\.local\.md)$"
            r"|(^|/)\.claude/settings\.local\.json$"
        ),
    ),
)
ALLOWED_ENV_EXAMPLES = re.compile(r"(^|/)\.env(\.[a-z0-9-]+)?\.example$")

# Exceptions for hand-written test inputs only, each scoped to one rule, one path and one line
# shape (like .gitleaks.toml): a real key on any other line or in any other file is still
# reported. There is no inline marker; a comment cannot exempt a line. The entries also cover
# the same lines in earlier commits, which the history scan reads.
ALLOWLIST: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    # a Windows upload path whose directory part the sanitiser must drop
    (
        "personal-path",
        re.compile(r"^tests/api/test_api_uploads\.py$"),
        re.compile(r'^\s*assert sanitize_filename\("C:(\\\\)Users\1x\1[^"\\/]*"\) == "[^"]*"'),
    ),
    # a non-sample DRF OC value for the prod startup check (no real law.go.kr key)
    (
        "secret-assignment",
        re.compile(r"^tests/config/test_config_env\.py$"),
        re.compile(r'^\s*_prod\(JETTAE_DRF_OC="[A-Za-z0-9-]{1,16}"\)'),
    ),
    # a strong-looking JWT secret for the prod example test (commits up to f0e66c0)
    (
        "secret-assignment",
        re.compile(r"^tests/config/test_env_examples\.py$"),
        re.compile(r'^\s*os\.environ\["JETTAE_JWT_SECRET"\] = "[A-Za-z0-9-]{32,40}"'),
    ),
)

# Personal absolute paths (Windows user profile, macOS/Linux home directories).
PERSONAL_PATH = re.compile(
    r"(?i)\b[A-Z]:[\\/]+Users[\\/]+(?!Public\b|Default\b|<|%|\$|\.\.\.|me\b)[^\\/\s\"'`]+"
    r"|/Users/(?!Shared\b|<|\$)[a-z0-9._-]+/|/home/(?!runner\b|<|\$)[a-z0-9._-]+/"
)


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int | None = None
    blob: str | None = None


def _is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER.search(value)) or len(set(value)) < 6


def _allowed(rule: str, path: str, line: str) -> bool:
    return any(r == rule and p.search(path) and shape.search(line) for r, p, shape in ALLOWLIST)


def scan_text(path: str, text: str, blob: str | None = None) -> Iterator[Finding]:
    lines = text.splitlines()
    for f in _scan_lines(path, lines, blob):
        if not _allowed(f.rule, path, lines[f.line - 1] if f.line else ""):
            yield f


def _scan_lines(path: str, lines: list[str], blob: str | None) -> Iterator[Finding]:
    for no, line in enumerate(lines, start=1):
        for rule, pat in CONTENT_RULES:
            m = pat.search(line)
            if m and not _is_placeholder(m.group(0)):
                yield Finding(rule, path, no, blob)
        for m in ASSIGNMENT.finditer(line):
            value = m.group("value")
            # a reference to another variable or a code expression is not a value
            if m.group("call") or _is_placeholder(value):
                continue  # a function call (e.g. a random generator) or an obvious example
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.()\[\]]*", value) and not re.search(
                r"\d", value
            ):
                continue
            yield Finding("secret-assignment", path, no, blob)
        for m in URL_CREDENTIAL.finditer(line):
            if not _is_placeholder(m.group("pw")):
                yield Finding("url-credential", path, no, blob)
        if PERSONAL_PATH.search(line):
            yield Finding("personal-path", path, no, blob)


def scan_path_name(path: str) -> Iterator[Finding]:
    if ALLOWED_ENV_EXAMPLES.search(path):
        return
    for rule, pat in PATH_RULES:
        if pat.search(path):
            yield Finding(rule, path)


def _git(root: Path, *args: str, input_: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args], input=input_, capture_output=True, check=True
    ).stdout


def publish_set(root: Path) -> list[str]:
    tracked = _git(root, "ls-files", "-z").split(b"\0")
    new = _git(root, "ls-files", "-z", "--others", "--exclude-standard").split(b"\0")
    return sorted({p.decode("utf-8") for p in tracked + new if p})


def _decode(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def scan_publish_set(root: Path, paths: Iterable[str]) -> Iterator[Finding]:
    for rel in paths:
        yield from scan_path_name(rel)
        p = root / rel
        if not p.is_file() or p.stat().st_size > 5_000_000:
            continue
        text = _decode(p.read_bytes())
        if text is not None:
            yield from scan_text(rel, text)


def scan_history(root: Path) -> tuple[int, int, list[Finding]]:
    """(commits, blobs, findings) for every object reachable from any ref."""
    try:
        commits = _git(root, "rev-list", "--all").split()
    except subprocess.CalledProcessError:
        return 0, 0, []
    if not commits:
        return 0, 0, []
    objs = _git(root, "rev-list", "--all", "--objects").decode("utf-8", "replace").splitlines()
    blob_paths: dict[str, str] = {}
    for line in objs:
        sha, _, path = line.partition(" ")
        if path:
            blob_paths.setdefault(sha, path)
    findings: list[Finding] = []
    seen: set[str] = set()
    for sha, path in blob_paths.items():
        kind = _git(root, "cat-file", "-t", sha).strip()
        if kind != b"blob":
            continue
        findings.extend(Finding(f.rule, f.path, None, sha) for f in scan_path_name(path))
        if sha in seen:
            continue
        seen.add(sha)
        data = _git(root, "cat-file", "blob", sha)
        if len(data) > 5_000_000:
            continue
        text = _decode(data)
        if text is not None:
            findings.extend(scan_text(path, text, blob=sha))
    return len(commits), len(seen), findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        files = publish_set(args.root)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"git unavailable: {e}", file=sys.stderr)
        return 2
    found = list(scan_publish_set(args.root, files))
    commits = blobs = 0
    if not args.no_history:
        commits, blobs, hist = scan_history(args.root)
        found.extend(hist)
    summary = {
        "publish_set_files": len(files),
        "history_commits": commits,
        "history_blobs": blobs,
        "findings": len(found),
        "by_rule": {
            r: sum(1 for f in found if f.rule == r) for r in sorted({f.rule for f in found})
        },
    }
    if args.json:
        print(json.dumps({"summary": summary, "findings": [asdict(f) for f in found]}, indent=2))
    else:
        for f in found:
            where = f"{f.path}:{f.line}" if f.line else f.path
            print(f"{f.rule}\t{where}" + (f"\tblob {f.blob[:12]}" if f.blob else ""))
        print(json.dumps(summary))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
