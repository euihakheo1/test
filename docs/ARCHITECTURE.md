# Architecture (modular monolith)

Python 3.12 · uv · FastAPI · Pydantic v2 · SQLAlchemy 2 + Alembic (SQLite dev / PostgreSQL prod) · DB-backed job queue + separate worker process · Typer CLI · MCP server (official `mcp` SDK) · optional LangGraph agent flows · Next.js (TypeScript) frontend in `frontend/`.

Dependency direction: `domain <- application <- adapters / interfaces`. Domain packages are pure Python.

```
src/jettae/
  config.py      Settings, .env loading, require_valid_environment(component)   (startup gate)
  domain/        money.py dates.py models.py status.py errors.py      (pure)
  rules/         registry.py kr_retail.py calendar_kr.py contract_term.py (pure; rule versions with source + effective dates)
  recon/         candidates.py matcher.py allocation.py                (pure; deterministic reconciliation)
  evidence/      deps.py invalidate.py snapshot.py links.py engine.py  (pure; document rows -> economic receivables, dependency graph, full vs incremental recompute)
  verify/        checks.py                                             (pure; citation exists, numbers equal, conservation)
  app/           ports.py services.py dto.py contracts.py doc_apply.py (use cases; depend on ports, not on adapters)
  ingest/        csvx.py xlsx.py xlsx_guard.py pdfx.py formats/ mapping.py pipeline.py (file -> rows/Facts with SourceSpan; resource limits before parsing)
  db/            orm.py orm_auth.py orm_investigations.py repos*.py jobs.py migrate.py runtime.py (SQLAlchemy adapters, job queue)
  store/         files.py                                              (content-addressed per-tenant blob store)
  llm/           base.py gateway.py replay.py anthropic.py openai.py fake.py budget.py budget_store.py (offline default; shared budget)
  ocr/           base.py vlm.py manual.py                              (table image -> rows; optional)
  agents/        flows.py planners.py tools.py investigations.py      (single vs role-split; web investigations)
  sources/       ftc/ bpi2019/ law/ contract/                         (real public data: fetch, manifest, convert)
  evals/         ftc_eval.py (+ ftc_inputs/rows/aggregate/report) bpi_eval.py contract_eval.py recompute_eval.py
  api/           main.py routes_*.py auth.py security.py deps.py      (FastAPI; cookie sessions + CSRF, Bearer tokens)
  worker.py                                                            (job runner, incl. investigate_decision)
  mcp_server.py                                                        (MCP tools over app.services; API tokens)
  demo.py                                                              (`jettae demo run`, dev only, real transcribed rows)
  cli.py                                                               (Typer root; loads .env, rejects unknown JETTAE_ENV; sub-apps registered lazily)
migrations/      alembic 0001..0006
frontend/        Next.js app (same-origin /api rewrites, Playwright E2E in frontend/e2e)
tests/           unit / api / ingest / integration / agents_web / config / ... (hand-written fixtures only)
data/            raw/ (gitignored downloads)  manifests/  seeds/ (real-data transcriptions with provenance)  results/
scripts/         secret_scan.py                                       (publish set + history scan, run in CI)
deploy/          Dockerfile docker-compose.yml run_pg_tests.py
docs/            SPEC.md ARCHITECTURE.md PROGRESS.md ADR/ ip/ runbook.md eval_protocol.md PUBLIC_USE.md security/
```

## Core contracts (stable interfaces; change a signature only together with its callers and a PROGRESS.md note)

- `Money(amount: int, currency: str = "KRW")` – minor units, arithmetic only between same currency, `Decimal` rate math with explicit rounding mode.
- `SourceSpan(doc_version_id, locator: dict, excerpt: str)` – locator e.g. `{"sheet": "Sheet1", "row": 12, "col": "D"}` or `{"page": 3, "char_start": 10, "char_end": 42}`.
- `Fact(id, tenant_id, kind, value, span, extractor, observed_at, valid_from, valid_to, supersedes)`.
- Ledger records: `Invoice`, `SettlementLine`, `BankTxn`, `Agreement`, each holding typed fields + `facts` references + `missing: set[str]`.
- `Allocation(source_id, target_id, amount, evidence)`; conservation: sum(allocations of a payment) <= payment amount; no double use.
- `ReconcileStatus`, `ReviewStatus` enums (SPEC §5).
- `RuleVersion(rule_id, version, effective_from, effective_to, known_from, source_url, params)`; `rules.kr_retail.compute_due(...)` returns `DueResult(base_date, due_date, delay_days, interest: Money, assumptions, variants)` or an `Insufficient(missing=[...], required_documents=[...])`.
- `Decision(id, tenant_id, subject_id, status, facts_used, rule_versions, computations, unresolved, required_documents, snapshot_hash, result_hash)`.
- `Approval(decision_id, result_hash, snapshot_hash, approved_by, approved_at)`; validity is computed, never overwritten.
- `evidence.deps.DependencyGraph`: edges doc_version->fact->computation->decision and *query scopes* (e.g. "agreements for counterparty X in period P") with result-set hash; `invalidate.plan(change) -> RecomputePlan(affected, reason, fallback_full: bool)`.
- `app.services` use cases: `register_document`, `ingest_document`, `run_analysis(tenant, snapshot)`, `apply_change`, `approve(decision_id, expected_result_hash)`, `export_report(decision_ids)`, `list_required_documents`.

## Status rules

- Missing base date (상품수령일/판매마감일) -> `INSUFFICIENT_EVIDENCE` with required documents; never substitute tax-invoice date.
- Calculation variants (rollover on/off, rounding) that cannot be resolved by known conditions -> return both variants + unresolved condition.
- Any change touching a decision's facts or query scopes -> that decision's current validity becomes `REVIEW_REQUIRED`; past approvals stay in history.

## Contracts added by the review fixes (2026-10-06)

- **Document application** (`app/doc_apply.py`, `DocumentApplier`): the only code that turns a parse
  result into ledger changes (worker and in-process `ingest.pipeline.ingest_document`). Each document
  has one current version (`DocumentHead`, table `document_heads`); a version becomes current only if
  it is at least as new as the current one, checked and written in the same tenant write transaction
  as the ledger change. Older versions, late jobs and retries are parsed and kept, never promoted.
  A recognised table with zero rows and no errors removes the document's previous rows; a parse
  failure or a file without a table never removes anything. Excluded rows / unread cells / 합계
  differences give `applied_needs_ack`: approval of a decision citing that version returns 409
  `document_ack_required` until `POST /document-versions/{id}/acknowledge`.
- **Mapping request** (`app/contracts.py`, `MappingRequest`): `{format_id?, columns: {field: int|null},
  options: {counterparty_override?, account_override?, self_brn?, direction?, accept_suggested?}}`,
  strict (no string/bool column numbers, no unknown keys, one column per field). Parse results are
  `ParseResult` (records, facts, issues with kind excluded/value, totals, row counts).
- **Receivables** (`evidence/links.py`, `domain.models.Receivable`): document rows are not
  receivables. Every settlement line is a receivable (basis = settlement line); a tax invoice is
  corroborating evidence only via the same counterparty + same normalised reference (exactly one
  SALE line) or a user `EvidenceLink`; never by equal amount/date. Uncertain invoices get an
  AMBIGUOUS decision with unresolved `evidence_link` and `evidence.counted = false` (not in totals,
  no payment allocation). Linked documents with different amounts give CONFLICT. Every decision has
  an `evidence` computation naming the basis document. `EvidenceLink` records are stored in the
  ledger repo (entity `evidence_link`, `app.ports.CONFIRMATION_ENTITIES`) and written only by
  `JettaeService.confirm_evidence_link / withdraw_evidence_link`
  (`POST /decisions/{id}/evidence-link`, `DELETE /evidence-links/{id}`).
- **Contractual due date** (`rules/contract_term.py`): computation `contractual_due` = statutory base
  date + agreement `payment_term_days`, no rollover, `interest` always None (the statutory rate is not
  applied to it); shown next to, never instead of, the statutory due date.
- **`as_of` / `known_at`**: `as_of` = unpaid-as-of date (payments after it are excluded, open
  tranches count delay days up to it); not a bitemporal restore of documents. `known_at` only selects
  rule versions.
- **LLM budget** (`llm/budget_store.py`): `Budget` reads a `BudgetStore` on every check; reservations
  are atomic across objects and processes (memory / file lock / SQL tables `llm_budget*`, migration
  0004). Service deployments set `JETTAE_LLM_BUDGET_DB` to the service database.

## Contracts changed by the second review (2026-10-06)

- **Unread tables** (`ingest/pipeline.py`, `RowCounts.tables_unread / unread_rows`): a table with data
  rows that no format recognises is *unread*, never "deleted". Such a version is `applied_needs_ack`
  (never `applied`); with zero recognised rows it is `not_applied_empty_unverified`.
- **Carry-over** (`DocumentApplier`, rule 7): a partially read version (excluded rows or unread
  tables) does not REMOVE the document's earlier records that have no counterpart; they stay, citing
  the version they came from (`ApplyOutcome.records_carried_over / carried_over`). Approval of any
  decision citing *any* version of a document whose head needs acknowledgment is blocked
  (`approval_blockers` checks the document head, not the cited version). The acknowledgment removes
  the carried-over records in the same transaction (`AckReport.removed`; the API response adds
  `records_removed` / `removed`).
- **Mapping per table** (`MappingRequest.table`): column indexes apply to the named table (and to
  other not-yet-recognised tables with the identical header row), never to a table recognised with
  confirmed confidence. An unnamed mapping on a file with several tables is accepted only when the
  unrecognised tables share one header row; otherwise `MappingContractError` (job: `bad_mapping`).
- **Mapping of the next version** (`worker._mapping_for`, `IngestJobResult.mapping: MappingSource`):
  job mapping > mapping confirmed for this version > mapping confirmed for the newest earlier
  version, carried over by header text (`inherited`). If it cannot be carried over (header row
  changed in a way the mapping depends on), the version is `NEEDS_MAPPING`, not auto-mapped.
- **Pending receivables** (`domain.models.PendingKind`, `Receivable.pending / missing`):
  `evidence_link` (invoice vs settlement line), `duplicate_line` (same counterparty + normalised
  reference, or settlement number + amount, in an earlier *other* document version; provenance =
  `Snapshot.document_origins`), `invoice_direction` (direction or counterparty unknown; status
  INSUFFICIENT_EVIDENCE, no confirmation choice - re-read with `self_brn`/`direction`).
  `EvidenceLink.document_entity` (`invoice` | `settlement_line`) lets the same endpoint confirm a
  duplicate line (`same_sale` = corroborating evidence of the earlier line, `separate_sale` = counted).
- **LLM budget ledger**: the live default is an absolute per-user path (`default_ledger_path`:
  `JETTAE_STATE_DIR` > `%LOCALAPPDATA%\jettae` > `$XDG_STATE_HOME/jettae` > `~/.local/state/jettae`);
  a relative `JETTAE_LLM_LEDGER` is refused in live mode. `FileLockBudgetStore` truncates an
  unfinished last line under the lock and raises `BudgetStoreCorrupt` (an `LLMError`) for damage
  before the last line.

## Contracts added for publication (2026-10-06)

- **Environment** (`config.py`): `JETTAE_ENV` in `dev | test | prod` (default `dev`); any other value
  stops every command. `.env` loading: `JETTAE_ENV_FILE`, else `./.env` of the working directory;
  real process variables win. `require_valid_environment(component)` (`api`, `worker`, `mcp`, `db`,
  `sources[:name]`) raises `SystemExit` listing problems by variable name (never values); the prod
  rules are in `environment_problems`. Called by the uvicorn app factory and `jettae api serve`
  (`api`), `jettae worker run`, `jettae mcp serve`, `jettae api migrate` and `jettae demo run`
  (`db`) and the `jettae sources` callback. The root CLI callback loads `.env` and rejects an
  unknown `JETTAE_ENV` for every other command.
- **Browser sessions** (`api/security.py`, `api/auth.py`, `db/orm_auth.py`, migration 0005): cookies
  `jt_access` (HttpOnly, Lax, `/api`), `jt_refresh` (HttpOnly, Strict, `/api/v1/auth`), `jt_csrf`
  (readable, Lax, `/`); Secure in prod; no tokens in response bodies. One `auth_sessions` row per
  login = refresh family = `sid` claim, checked on every request; refresh rotates, reuse of a rotated
  token or logout revokes the session. Exception: a token rotated less than
  `JETTAE_REFRESH_REUSE_GRACE_S` (default 20 s) ago in a still-valid session gets a new access cookie
  only (no refresh cookie, the family is not forked) - browser tabs share one cookie jar and refresh
  together. `/auth/refresh` is limited per session (`JETTAE_REFRESH_PER_MINUTE`), never per client
  IP; signup/login use the per-IP limiter. The web client logs the user out only on 401/403 from
  refresh; 429/5xx/network errors leave the session state `error`. Unsafe cookie requests need `X-CSRF-Token` = `jt_csrf`
  (session-bound HMAC) else 403 `csrf_failed`; Bearer (API token / JWT) requests are exempt. The
  frontend never stores tokens and calls `/api/v1` same-origin through Next.js rewrites.
- **XLSX limits** (`ingest/xlsx_guard.py`, `XlsxLimits`): zip entry count/size/ratio, an encoding
  sniff of every member (XML only as UTF-8 or BOM-marked UTF-16; UTF-16 without BOM, UCS-4, EBCDIC
  and other declared encodings refused), DTD refusal, and element caps on every part openpyxl
  parses, chosen the way openpyxl chooses them (content types, workbook relationships, fixed
  styles/docProps paths, everything reachable from chart sheets), never by root-element name:
  worksheets (rows, columns, cells, row elements, rows x columns area, merged ranges and area),
  shared strings (per table and per string), styles (style records), manifest/rels/workbook/other
  parts (per part and in total). All run before openpyxl touches a cell; the `<dimension>` tag is
  never trusted. A limit gives `FAILED` with a limit code, a malformed file
  `CORRUPT`, never an empty "no transactions" result.
- **Agent investigations** (`agents/investigations.py`, `db/repos_investigations.py`,
  `api/routes_investigations.py`, migration 0006): the API creates the `agent_investigations` row
  and its `investigate_decision` job in one transaction (`JOB_TYPES` of `POST /jobs` excludes this
  type on purpose). `offline` = deterministic heuristic planner, no LLM; `replay`/`live` go through
  the LLM gateway; `live` is decided by the worker (`live_capability`) and otherwise ends
  `refused`. Findings are built by code from the stored decision and typed tool results, never from
  model text, and never change engine numbers.
- **Demo** (`demo.py`): builds settlement and bank CSVs by copying values of
  `data/seeds/ftc_rows.csv` (rows that cannot be copied exactly are skipped and counted), creates a
  tenant/user through `AuthService`, uploads through `JettaeService.register_document` + the job
  queue and runs one worker pass. `dev` only; the password is printed once and stored nowhere.
- **Publication** (`scripts/secret_scan.py`, `.gitignore`, `docs/PUBLIC_USE.md`): the publish set
  (`git ls-files` + untracked, not ignored) and every blob reachable from any ref are scanned for key
  formats, secret assignments, URL credentials, personal absolute paths and forbidden paths (env
  files, databases, logs, caches, `var/`, blob store, LLM replay cache and budget ledger,
  `data/raw/`, `data/results/`, downloaded prior-art PDFs, per-user tool settings; a test checks
  that every sensitive `.gitignore` entry is a path rule). Exceptions only through `ALLOWLIST`
  (one rule, one path, one line shape); no inline marker. Findings print rule, path and line only.
- **Hermetic tests** (`tests/_plugins/jettae_testenv.py`, loaded by `-p` in `pyproject.toml`):
  removes `JETTAE_*` (except `JETTAE_TEST_PG_URL`), `ANTHROPIC_*`, `OPENAI_*` and points
  `JETTAE_ENV_FILE` at an empty file before collection, so a shell prepared for live LLM calls or
  a `./.env` cannot change test results or expose keys to tests.
- **Line endings** (`.gitattributes`): text files are LF on every OS, so evaluation provenance
  hashes (seeds, engine sources) are the same in a Windows checkout; evaluation writers write LF.

