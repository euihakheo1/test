# Rules for coding agents working on this repo

Read `docs/SPEC.md` (product spec, Korean) and `docs/ARCHITECTURE.md` (module contracts) first;
`README.md` is the user-facing install/run guide and must stay runnable as written.

## Environment

All commands run from the repository root. Paths in this file are relative; machine-specific
locations come from environment variables, never from absolute paths written into the repo.

```bash
export UV="${UV:-uv}"                      # path to the uv executable if it is not on PATH
# Windows with long paths disabled: keep the venv on a short path outside the repo
# export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/jettae"
"$UV" sync --frozen --all-extras           # install from uv.lock
"$UV" run pytest -q                        # tests
"$UV" run jettae --help                    # CLI
```

- Never use a system or Anaconda Python directly; always go through `uv run`.
- Keep file paths short (Windows MAX_PATH 260 when long paths are disabled). No deeply nested dirs.
- Local DB = SQLite. Code must stay PostgreSQL-compatible; PostgreSQL tests need a throwaway
  database (`JETTAE_TEST_PG_URL`, or `uv run --with pgserver python deploy/run_pg_tests.py var/pgtest`).
- Network may be available (curl/httpx). `data.4tu.nl` may answer `{"status": "maintenance"}`:
  handle it, do not fake data.
- Write helper scripts and temporary files outside the repository (or under `var/`, which is ignored).

## Hard rules

1. **No synthetic datasets.** Evaluation uses only real public data (see SPEC §3). Small hand-written
   test fixtures are allowed only under `tests/` and `frontend/e2e/fixtures/` (E2E inputs), never in
   evaluation results.
2. Never fabricate numbers, downloads, results or model performance. If something could not be run,
   say so in `docs/PROGRESS.md` with the exact command.
3. Money: integer KRW minor units or `Decimal`; never `float`. Dates: `datetime.date` for business
   dates, UTC-aware `datetime` for system time.
4. Domain code (`src/jettae/domain`, `rules`, `recon`, `evidence`, `verify`) must not import FastAPI,
   SQLAlchemy, LangGraph, MCP or any LLM SDK.
5. Offline by default. No paid LLM call unless `JETTAE_LLM_MODE=live` AND `JETTAE_LLM_BUDGET_KRW>0`
   are set. Tests and CI never make paid calls.
6. File ownership: only edit files in the directories your task assigns to you. `pyproject.toml` is
   owned by the integrator; if you need a dependency, append a line to `docs/deps/<your-task>.txt`.
7. Every task ends with `uv run pytest -q` passing for your area and an entry in `docs/PROGRESS.md`
   (append-only: what was done, commands run, what was not verified).
8. Output wording rule (user-facing): describe differences, required documents, unconfirmed
   conditions, and sources. Never output legal conclusions such as "위법" or "받을 수 있다".
9. **Publication.** Never commit real secrets, user uploads, databases, LLM caches/ledgers, logs,
   raw downloads (`data/raw/`) or personal absolute paths. Example config files contain only
   obviously fake values (`change-me-...`). Never print secret values in output or reports (report
   rule ids, counts and paths only). Run `python scripts/secret_scan.py` before committing.
10. Do not add or change a `LICENSE` file; no license has been chosen (`docs/PUBLIC_USE.md`).
