# Release verification (first public commits)

Date: 2026-10-06. Machine: Windows 11, Git Bash, uv 0.12.23, Node 24.16.0 / npm 11.13.0,
git 2.51.2.windows.1. Nothing was pushed.

| Commit | Content |
|---|---|
| `af18802852faba35ead224bd7f673186f032b81c` | Initial public release (365 files) |
| `9cb638fc2e87cf66cde24f84df383de99879a0cd` | **Verified commit.** `.gitleaks.toml` allowlist fix for `dir` mode / CRLF checkouts |
| the commit that adds this file | Docs only (this file and a `docs/PROGRESS.md` entry); not part of the verified tree |

All numbers below were observed on `9cb638f` unless a line says otherwise. Passing these checks
shows only the listed behaviours; it does not establish production security, deployment
correctness or LLM/Agent/OCR accuracy.

## 1. What is published and what is not

Policy: `docs/PUBLIC_USE.md` §3 and `.gitignore`. Security and operations source code is published;
real secrets, user uploads, databases, blob store, LLM replay cache and budget ledger, logs,
caches, build/E2E output, raw downloads (`data/raw/`) and regenerable evaluation JSON
(`data/results/`) are not. `data/manifests/` (relative paths only) and `data/seeds/` (transcriptions
with provenance) are published. After every step of the fresh-clone run below,
`git status --short` was empty: everything the run created (`.env`, `var/`, `.hypothesis/`,
`.pytest_cache/`, `frontend/node_modules/`, `frontend/.next/`, `frontend/next-env.d.ts`,
`frontend/tsconfig.tsbuildinfo`, `frontend/.e2e-tmp/`) is ignored.

## 2. Secret and personal-data scans

Values are never printed; reports give rule ids, counts and paths only.

| Check | Command | Result |
|---|---|---|
| gitleaks 8.30.1 (Windows x64 release zip; SHA-256 matched the release checksum file) on the staged tree before the first commit | `gitleaks git --staged --redact -c .gitleaks.toml` | first run 1 `generic-api-key` (`frontend/e2e/flow.spec.ts`, the throwaway E2E sign-up password) → allowlisted by rule + path + line shape in `.gitleaks.toml`; then 0 |
| gitleaks, full history | `gitleaks git --redact -c .gitleaks.toml --log-opts=--all .` | 2 commits, 0 findings |
| gitleaks, committed tree exported with `git archive HEAD` | `gitleaks dir . --redact -c .gitleaks.toml` | 0 findings (on `af18802` this reported the E2E password again because of `\` paths and CRLF; fixed in `9cb638f`) |
| allowlist scope check (temporary copy) | same line copied to another file + a random key appended to `flow.spec.ts` | both reported (2 findings): the allowlist hides only the one line |
| repository scanner | `uv run python scripts/secret_scan.py` | publish set 365 files, history 2 commits / 366 blobs, 0 findings |
| personal data, committed tree (365 files) | ad-hoc pattern scan (not in the repo): Windows/POSIX home paths, local account name, agent scratch folders, e-mail, Korean mobile/landline, resident registration number, key formats | home paths 0, account name 0, scratch folders 0, phone 0 (1 hit is a wheel hash in `uv.lock`), RRN 0, key formats 0; e-mail 90 hits in 25 files, all `example.com` / `*.example` test addresses or a `db.internal` host in example DSNs; 9 `AppData` hits are `%LOCALAPPDATA%`/`%USERPROFILE%` references |
| personal paths, full history | `git log -p --all` grep for drive-letter and MSYS user-profile paths, the account name, scratch folders, the user's e-mail | 1 hit: a filename-sanitising test input whose user-profile folder is the placeholder `x` |

Commit identity: `jettae-maintainer <jettae-maintainer@users.noreply.github.com>` (repo-local
config, author and committer). Repo-local `core.longpaths=true` was needed because object writes
failed with "Filename too long" in the long working path.

## 3. Fresh clone, README steps

`git clone <repo> "$HOME/.cache/jtc"` (short path), new virtualenv via `UV_PROJECT_ENVIRONMENT`,
`uv` on `PATH`. Commands as written in README; the demo credential lines were filtered out of the
log.

| Step (repository root unless noted) | Result |
|---|---|
| `uv sync --frozen --all-extras` | exit 0 |
| `cp .env.example .env` | exit 0 |
| `uv run jettae api migrate` | "database upgraded to head"; `uv run alembic heads` → `0006_investigations (head)` |
| `uv run pytest -q` | 614 passed, 8 skipped (PostgreSQL-only, `JETTAE_TEST_PG_URL` not set), 0 errors |
| `uv run jettae demo run --out-dir var/demo` | exit 0; 77 rows used, 18 excluded and counted; 77 results: 54 MATCHED, 23 INSUFFICIENT_EVIDENCE |
| `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` | clean; 264 files formatted; 152 source files, no issues |
| `frontend/`: state before install | `.next`, `next-env.d.ts`, `node_modules` absent |
| `npm ci` | exit 0 (reports 5 high from the dev-only lint chain; see `docs/security/dependency-audit.md`) |
| `npm run lint` | exit 0 |
| `npm run typecheck` (`next typegen && tsc --noEmit`, no `.next` present) | exit 0 |
| `npm test` | 40 passed, 0 failed |
| `npm run build` | exit 0, 12 routes listed (incl. `/_not-found`) |
| `npm run e2e:install` | exit 0 (chromium was already in the per-user Playwright cache, so the download path was not exercised) |
| `npm run e2e` | 3 passed (full flow incl. Agent investigation offline; HttpOnly cookies + CSRF + logout; refresh rotation + tenant isolation) |

The same sequence also passed on `af18802` (614 passed / 8 skipped; E2E 3 passed); the clone was
deleted and recreated from scratch for `9cb638f`.

## 4. Not verified

- GitHub Actions has not run (nothing pushed). The new gitleaks CI step (Linux tarball, pinned
  SHA-256) was checked only with actionlint 1.7.12 + shellcheck. Linux behaviour, the `postgres:16`
  service job and `playwright install --with-deps` are unconfirmed.
- PostgreSQL tests were skipped in this run (earlier runs on a temporary pgserver PostgreSQL 16 are
  recorded in `docs/PROGRESS.md`).
- API + worker + web started by hand from the README terminals and the live-LLM / prod example
  files were exercised in the previous phase (`docs/PROGRESS.md`), not repeated on this commit.
- Docker/Compose, a real HTTPS deployment behind a proxy, paid LLM calls, LLM/Agent/OCR quality,
  the E4 BPI 2019 evaluation.
- Pattern scans find known shapes only; they do not prove the absence of every secret or personal
  datum. `data/seeds/` contains company names from public FTC decisions by design.
- GitHub private vulnerability reporting must be enabled by the repository owner (`SECURITY.md`).
- No license has been chosen (`docs/PUBLIC_USE.md`); publishing the repository does not grant reuse
  rights. `docs/ip/` (research/patent working notes) and `docs/SPEC.md` §7 become public with the
  repository; whether that is intended is the owner's decision.
