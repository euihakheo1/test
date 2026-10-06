# Runbook: API, worker, database

This covers the API, worker and database of 제때받기 (`jettae`). Commands run from the repository
root with `uv` on PATH (set `UV_PROJECT_ENVIRONMENT` first on Windows machines with long paths
disabled; see `AGENTS.md`). The user-facing guide is `README.md`.

## 1. Processes

| Process | Command | Notes |
|---|---|---|
| API | `jettae api serve [--host --port --workers]` | FastAPI on uvicorn; versioned under `/api/v1`; OpenAPI at `/api/v1/openapi.json`, docs at `/api/v1/docs` |
| Worker | `jettae worker run [--concurrency N] [--once]` | DB-backed queue; job types `ingest_document`, `run_analysis`, `apply_change` (generic `POST /jobs`) and `investigate_decision` (created only by `POST /decisions/{id}/investigations`) |
| Migrations | `jettae api migrate [REV] [--down]` or `alembic upgrade head` | Same Alembic scripts on SQLite and PostgreSQL |
| OpenAPI dump | `jettae api openapi --out openapi.json` | Uses a temporary SQLite; no DB needed |
| MCP | `jettae mcp serve [--transport stdio\|streamable-http]` | API tokens only (no cookies) |

Run the API and the worker as separate processes, from the same code, working directory and
settings. Several workers are fine; jobs are claimed with leases, so no job runs twice at once.

## 2. Configuration

### Loading order

1. `JETTAE_ENV_FILE`, if set, names the only file read (relative to the working directory; a missing
   file is an error).
2. Otherwise `./.env` in the working directory is read when it exists.
3. Variables already present in the process environment win over file values.

The file is loaded into the process environment, so adapters that read `os.environ` directly
(LLM, OCR, sources) see the same values. The examples are `.env.example` (local, dev),
`.env.live-llm.example` (paid LLM), `.env.vllm.example` (private local inference) and
`.env.prod.example` (prod); all secret slots hold fake
`change-me-...` values.

### Startup check

`JETTAE_ENV` must be `dev`, `test` or `prod`; any other value stops every `jettae` command. With
`JETTAE_ENV=prod`, `jettae.config.require_valid_environment(component)` refuses to start when:

| Component | Refused when |
|---|---|
| `api`, `mcp` | `JETTAE_JWT_SECRET` missing, shorter than 32 bytes, a known placeholder or low-variety |
| `api`, `worker`, `mcp`, `db` (migrations, demo) | `JETTAE_DATABASE_URL` is not PostgreSQL, or its password contains a placeholder marker (`change-me`, `example`, ...) |
| `api` | `JETTAE_ALLOWED_ORIGINS` empty or not https; `JETTAE_COOKIE_SECURE=false` |
| `sources ftc`, `sources law` | `JETTAE_DRF_OC` missing, `test` or a placeholder |
| `api`, `worker`, `mcp` with `JETTAE_LLM_MODE=live` | `JETTAE_LLM_BUDGET_KRW` not a finite number in (0, 10 000 000 000] (`Infinity`, `1e400`, `NaN` refused), or `JETTAE_LLM_BUDGET_DB` not PostgreSQL or with a placeholder password |
| `api`, `worker`, `mcp` with `JETTAE_LLM_MODE=local` | Provider is not `vllm`, endpoint is outside the allowed private addresses, or (in prod) the vLLM key is missing, a placeholder or weak |

Local mode/provider/endpoint checks also run in dev/test. The message lists every problem by variable name and never prints values. A wildcard origin is
refused in every environment. `jettae demo run` additionally refuses anything but `dev`.

### Variables (`JETTAE_` prefix)

| Variable | Default | Meaning |
|---|---|---|
| `JETTAE_ENV` | `dev` | `dev`, `test` or `prod` (see above) |
| `JETTAE_ENV_FILE` | (unset) | Settings file to load instead of `./.env` |
| `JETTAE_DATABASE_URL` | `sqlite:///./var/jettae.db` | SQLAlchemy URL (relative to the working directory). Prod: `postgresql+psycopg://user:pw@host:5432/jettae` |
| `JETTAE_BLOB_DIR` | `./var/blobs` | Per-tenant, content-addressed file store (user uploads; back it up with the DB) |
| `JETTAE_MIGRATIONS_DIR` | `<repo>/migrations` | Used by `/ready` and `jettae api migrate` |
| `JETTAE_JWT_SECRET` | (unset) | HS256 signing key. Unset in `dev`/`test`: a per-process random key and a warning; sessions end on restart |
| `JETTAE_ACCESS_TOKEN_TTL_S` | `900` | Access JWT lifetime (cookie `jt_access`) |
| `JETTAE_REFRESH_TOKEN_TTL_S` | `1209600` | Refresh token lifetime (14 days); rotated on every use |
| `JETTAE_SESSION_MAX_AGE_S` | `2592000` | Absolute session lifetime (30 days); refresh cannot extend it |
| `JETTAE_ALLOWED_ORIGINS` | (empty) | Exact browser origins allowed with credentials (CORS) and by the Origin check on unsafe requests. Required (https) in prod. `JETTAE_CORS_ORIGINS` is the older name |
| `JETTAE_COOKIE_SECURE` | on in prod, off otherwise | `Secure` flag of the session cookies; `false` is refused in prod |
| `JETTAE_ARGON2_TIME_COST` / `_MEMORY_KIB` / `_PARALLELISM` | `3` / `65536` / `4` | Password hashing cost |
| `JETTAE_PASSWORD_MIN_LENGTH` | `10` | |
| `JETTAE_AUTH_IP_PER_MINUTE` | `30` | signup/login per client IP per minute (429 + `Retry-After`); `0` disables. Per API process |
| `JETTAE_REFRESH_PER_MINUTE` | `30` | `POST /auth/refresh` per **session** per minute (not per IP: one client's failed logins must not block other users' refresh); `0` disables |
| `JETTAE_REFRESH_REUSE_GRACE_S` | `20` | A rotated refresh token presented again within this window (tabs refreshing together) gets a new access cookie only; later reuse ends the session. `0` = strict reuse detection |
| `JETTAE_LOGIN_MAX_FAILURES` / `JETTAE_LOGIN_LOCKOUT_S` | `5` / `900` | Failed logins per email inside the window, then 429. Per API process |
| `JETTAE_PUBLIC_IP_PER_MINUTE` | `30` | `POST /api/v1/public/due` (no login) per client IP per minute |
| `JETTAE_MAX_UPLOAD_BYTES` | `20971520` | Upload size limit, enforced while streaming (413) |
| `JETTAE_MAX_JSON_BODY_BYTES` | `2097152` | Limit for non-multipart bodies |
| `JETTAE_UPLOAD_EXTENSIONS` | `.csv,.txt,.xlsx,.pdf` | Accepted extensions; content is also checked by magic bytes (415) |
| `JETTAE_WORKER_CONCURRENCY` | `2` | Thread slots per worker process |
| `JETTAE_WORKER_POLL_S` | `1.0` | Idle poll interval |
| `JETTAE_JOB_LEASE_S` | `60` | Lease length. Heartbeats renew it every lease/3; an expired lease means the worker crashed and another worker resumes the job |
| `JETTAE_JOB_MAX_ATTEMPTS` | `5` | Includes attempts lost to crashes |
| `JETTAE_JOB_BACKOFF_BASE_S` / `_MAX_S` | `2` / `300` | Retry delay: `base * 2^(attempt-1)`, capped, with jitter in [0.5, 1] |
| `JETTAE_API_HOST` / `JETTAE_API_PORT` | `127.0.0.1` / `8000` | |
| `JETTAE_INGEST_ENTRYPOINT` | `jettae.ingest.pipeline` | Module providing `parse_document(...)` and `suggest_mapping(...)` |
| `JETTAE_DRF_OC` | (unset) | 법제처 DRF key for `sources ftc/law`; `test` is the public sample key (dev only) |
| `JETTAE_LLM_*`, `JETTAE_TENANT_SETTINGS`, `JETTAE_OCR_PROVIDER` | offline, budget 0 | Paid LLM settings: README "실제 LLM 실행" and `.env.live-llm.example` |
| `JETTAE_LLM_BUDGET_DB` | (unset) | **Set it to the service database URL in a service deployment** that allows live calls (API + workers): every process then reserves spending in the `llm_budget*` tables (migration `0004`) in one transaction, so all share one cumulative limit. Unset: a file ledger (`JETTAE_LLM_LEDGER`, absolute path in live mode; default `llm_budget.jsonl` under `JETTAE_STATE_DIR`, else `%LOCALAPPDATA%\jettae`, `$XDG_STATE_HOME/jettae` or `~/.local/state/jettae`), shared only by the processes of one OS user on one disk |
| `JETTAE_LLM_BUDGET_ID` / `JETTAE_LLM_RESERVATION_TTL_S` | `default` / `900` | Budget row; after the TTL an unsettled reservation (crashed process) still counts as possibly billed |

uvicorn itself reads `FORWARDED_ALLOW_IPS` (default `127.0.0.1`): the proxies whose
`X-Forwarded-*` headers are trusted (`jettae api serve` enables proxy headers). The Compose file
sets it to its network gateway (section 7).

## 3. Local quick start (SQLite)

```bash
cp .env.example .env
uv run jettae api migrate                 # creates ./var/jettae.db
uv run jettae api serve --port 8000       # terminal 1
uv run jettae worker run                  # terminal 2
curl -s http://127.0.0.1:8000/api/v1/ready   # {"status":"ok", ...} once migrations are at head
```

Minimal cookie session with curl (the browser does the same through the Next.js rewrites). The
upload uses the demo file written by `uv run jettae demo run --out-dir var/demo`; any CSV works:

```bash
B=http://127.0.0.1:8000/api/v1
curl -s -c var/cj.txt -H 'content-type: application/json' -X POST "$B/auth/signup" \
  -d '{"email":"me@example.com","password":"correct horse battery","tenant_name":"Example Co"}'
CSRF=$(awk '$6=="jt_csrf"{print $7}' var/cj.txt)
curl -s -b var/cj.txt -c var/cj.txt -H "X-CSRF-Token: $CSRF" -H 'Idempotency-Key: up-1' \
  -X POST "$B/documents" -F file=@var/demo/demo_settlement.csv -F kind=settlement
curl -s -b var/cj.txt -c var/cj.txt -H "X-CSRF-Token: $CSRF" -H 'content-type: application/json' \
  -X POST "$B/jobs" -d '{"type":"run_analysis","params":{"as_of":"2025-11-01"}}'
curl -s -b var/cj.txt "$B/decisions"
curl -s -b var/cj.txt -c var/cj.txt -H "X-CSRF-Token: $CSRF" -X POST "$B/auth/logout"
rm var/cj.txt
```

SQLite notes: WAL mode and `busy_timeout=30s` are enabled. Write transactions use `BEGIN IMMEDIATE`,
which serialises all writers. Fine for one developer; use PostgreSQL for anything shared.

## 4. API conventions

- **Browser auth (cookies).** Login/signup set three cookies and return no tokens in the body:

  | Cookie | HttpOnly | SameSite | Path | Purpose |
  |---|---|---|---|---|
  | `jt_access` | yes | Lax | `/api` | access JWT (15 min) |
  | `jt_refresh` | yes | Strict | `/api/v1/auth` | refresh token, rotated on every `POST /auth/refresh` |
  | `jt_csrf` | no | Lax | `/` | CSRF token the page echoes as `X-CSRF-Token` |

  - `Secure` in prod; host-only (no `Domain`).
  - POST/PUT/PATCH/DELETE authenticated by cookie need `X-CSRF-Token` equal to `jt_csrf` (an HMAC
    bound to the session), else 403 `csrf_failed`. This includes refresh and logout.
  - Each login is an `auth_sessions` row (migration `0005`); its id is the refresh-token family and
    the `sid` claim of every access token, checked on every request. Logout or reuse of an old
    refresh token revokes the whole session, including access tokens already issued.
  - With `JETTAE_ALLOWED_ORIGINS` set, unsafe requests whose `Origin` is not listed get 403.
- **CLI/MCP auth (Bearer).** `Authorization: Bearer <token>` with an API token `jtk_...` (create:
  `POST /auth/api-tokens`, admin role or higher; only a hash is stored) or an access JWT. Bearer
  requests are exempt from the CSRF check. API tokens are never accepted from a cookie.
- **Tenant** comes only from the credential; tenant ids in bodies or headers are ignored. Roles:
  `viewer` (read) < `member` (upload, jobs, changes, approve, investigations) < `admin` (API tokens)
  < `owner`.
- **Errors.** `{"error":{"code","message","details","request_id"}}`.
  - Objects of another tenant answer `404`.
  - `409 stale_result`: the approval's `expected_result_hash` is not the current result.
  - `409 report_not_valid`: an export with `require_approved` included decisions that are not
    currently approved.
  - `409 document_ack_required`: the decision cites a document whose current version was applied
    only partially and nobody acknowledged it (`POST /document-versions/{id}/acknowledge`). Records
    of earlier versions that the partial version could not read are kept until then; the
    acknowledgment removes them (`records_removed`).
- **Evidence links.** A tax invoice that may be the same sale as a settlement line is never linked
  by equal amount/date: its decision is `AMBIGUOUS` with `unresolved=["evidence_link"]` until
  `POST /decisions/{id}/evidence-link {expected_result_hash, relation: same_sale|separate_sale,
  settlement_line_id?}`. `GET /evidence-links` lists confirmations; `DELETE /evidence-links/{id}`
  withdraws one. Both recompute incrementally.
- **Agent investigations.** `POST /decisions/{id}/investigations {strategy: single|roles, mode:
  offline|replay|live|local}` -> 202 `{investigation_id, job_id}`; `GET /decisions/{id}/investigations`;
  `GET /investigations/capabilities`. `live` runs only when the worker's settings allow it
  (`JETTAE_LLM_MODE=live`, budget > 0, and in prod a PostgreSQL `JETTAE_LLM_BUDGET_DB`); otherwise
  the investigation ends `refused` without a provider call. `local` requires server mode `local`,
  provider `vllm` and a private endpoint; a browser request cannot enable it. Local inference
  has no external API charge and does not enable paid providers. Findings never change engine numbers.
- **Idempotency.** `Idempotency-Key` works on `POST /documents`, `/jobs`, `/changes`,
  `/changes/upload`, `/decisions/{id}/approvals`, `/decisions/{id}/evidence-link`,
  `/decisions/{id}/investigations` and `/document-versions/{id}/mapping`. Same key and request:
  replay (`Idempotent-Replayed: true`); different request: 422; in flight: 409. Uploads are also
  content-idempotent (identical bytes: `200 duplicate=true`).
- **Pagination.** `?limit=&cursor=`; the response carries `next_cursor`.

## 5. Migrations

```bash
uv run jettae api migrate                  # upgrade to head (JETTAE_DATABASE_URL)
uv run jettae api migrate 0004 --down      # downgrade to a revision ("base" = empty)
uv run alembic heads                       # must show exactly one head (0006_investigations)
uv run alembic upgrade head                # same, via alembic.ini (URL from env)
uv run alembic -x url=sqlite:///var/x.db revision --autogenerate -m "short_name"
uv run alembic upgrade head --sql          # print DDL (offline), e.g. for review
uv run alembic -x url=sqlite:///var/x.db check   # models and migrations agree
```

Rules:
- Use only generic types (String, Text, Integer, BigInteger, Boolean, JSON, DateTime(tz)).
- `render_as_batch` is on for SQLite.
- Keep revision file names short (Windows MAX_PATH, `truncate_slug_length = 24`).
- Test both directions on SQLite (`tests/api/test_api_migrations.py`) and PostgreSQL (section 9).
- `/ready` returns 503 until the database is at the head revision.

Revisions: `0001` initial; `0002` document content dedupe (section 12); `0003` document heads and
`0004` shared LLM budget (section 13); `0005_auth_sessions` browser sessions (section 14);
`0006_investigations` Agent investigations (section 14).

## 6. Backup and restore

The database and the blob directory together are the state. Blobs are content-addressed and
append-only, so **back up the database first, then the blobs**.

PostgreSQL:

```bash
pg_dump -Fc -d "$PGURL" -f jettae-$(date +%F).dump
tar -C /data -czf blobs-$(date +%F).tgz blobs           # docker: volume "blobs"
# restore into an empty database
pg_restore --clean --if-exists -d "$PGURL" jettae-YYYY-MM-DD.dump
tar -C /data -xzf blobs-YYYY-MM-DD.tgz
jettae api migrate          # brings an older dump up to the current head
```

SQLite (online, safe while the API runs):

```bash
uv run python -c "import sqlite3; s=sqlite3.connect('var/jettae.db'); d=sqlite3.connect('var/backup.db'); s.backup(d); d.close()"
cp -r var/blobs var/blobs-backup
```

Restore: stop the API and worker, copy `backup.db` over `jettae.db` (remove `jettae.db-wal` and
`jettae.db-shm`), restore the blob directory, then start again. Backups contain user data: keep
them out of the repository and off shared drives.

After a restore, reads re-verify blob integrity: a corrupted file raises `BlobIntegrityError` and is
never served. Approvals are append-only and decisions keep their history.

## 7. Deploy (Docker Compose with PostgreSQL)

`deploy/`: `Dockerfile` (one image for API and worker), `docker-compose.yml` (`db` postgres:16,
one-shot `migrate`, `api`, `worker`, all `JETTAE_ENV=prod`), `.env.example`, `Dockerfile.dockerignore`.
The base file contains only the backend. Add `compose.web.yml` for the Next standalone server
and Caddy HTTPS proxy, and `compose.vllm.yml` for private GPU inference. The ordered setup is
in [GETTING_STARTED.md](GETTING_STARTED.md). These deployment overlays have not been run in this
review environment; see [FINAL_VERIFICATION.md](FINAL_VERIFICATION.md).

```bash
cd deploy && cp .env.example .env    # replace every change-me value and the origin
docker compose up -d --build         # migrate runs before api/worker start
docker compose logs -f api worker    # JSON lines, one access line per request (X-Request-ID)
curl -s http://127.0.0.1:8000/api/v1/ready
docker compose up -d --scale worker=3
```

**Docker was not run on the development machine (Docker is not installed there).** The Dockerfile
and compose file have not been built or started; only the YAML was parsed
(`tests/config/test_ci_workflow.py`).

Client IPs: the host's reverse proxy reaches the published port, and the API container sees the
connection coming from the Compose network gateway, not from `127.0.0.1`. The compose file pins
the network (`JETTAE_COMPOSE_SUBNET`, default `172.31.250.0/24`) and sets the API's
`FORWARDED_ALLOW_IPS` to its gateway (`JETTAE_PROXY_GATEWAY`, default `172.31.250.1`), so only
that hop's `X-Forwarded-For` is trusted. Change both together if the subnet collides. Without this
every client would share one IP for the signup/login limit. This reasoning follows Docker's port
publishing; it was not observed on a running Docker host.

Production layout: a TLS reverse proxy on one domain sends `/api/` to the API (published on the
host's loopback only) and everything else to `next start` (built with `JETTAE_API_ORIGIN` = the
internal API URL). Do not let `/api/` go through the Next.js rewrite in production: Next adds no
`X-Forwarded-For`, so the API would see every browser as `127.0.0.1`. Set `JETTAE_ALLOWED_ORIGINS` to that https origin. Secrets come from the
environment or a secret store, never from files in the repository.

## 8. Rollback

1. **Application only (no schema change):** redeploy the previous image tag. Jobs in flight are
   resumed by the new workers after their lease expires.
2. **Release with a migration:** stop the workers; run `jettae api migrate <previous_rev> --down`
   with the *new* image (the downgrade code lives there); deploy the old image and start the
   workers. If a downgrade would drop data (e.g. `0006` drops `agent_investigations`, `0005` drops
   `auth_sessions` and so ends every browser session), restore from the backup taken before the
   release instead.
3. Always back up the database and blobs before running migrations in production.

## 9. Tests

```bash
uv run pytest -q                                          # SQLite; PostgreSQL-only tests skip
uv run --with pgserver python deploy/run_pg_tests.py var/pgtest   # throwaway PostgreSQL via pgserver
```

`deploy/run_pg_tests.py` starts a throw-away PostgreSQL through `pgserver` (not a project
dependency; `uv run --with` adds it temporarily) and runs `tests/api/test_api_postgres.py` and the
PostgreSQL case of `tests/llm_agents/test_la_budget_store.py`: migration round trip and metadata
diff, API flow with isolation and approval 409, concurrent `FOR UPDATE SKIP LOCKED` claims,
incremental = full recompute, refresh-token rotation race, NUL byte in an uploaded CSV, document
versions/evidence links, carry-over and duplicate lines, shared LLM budget across processes.

The test run is hermetic (`tests/_plugins/jettae_testenv.py`): it drops `JETTAE_*` (except
`JETTAE_TEST_PG_URL`), `ANTHROPIC_*` and `OPENAI_*` and ignores `JETTAE_ENV_FILE` and `./.env`.

PostgreSQL tests are skipped unless `JETTAE_TEST_PG_URL` is set, and they **drop the `public` schema**
of the target database: point it only at a throwaway database. CI uses a `postgres:16` service
container (`.github/workflows/ci.yml`).

## 10. Operational notes

- `GET /api/v1/health`: process alive. `GET /api/v1/ready`: database, migration head, blob
  directory writable.
- Jobs: `GET /api/v1/jobs?status=failed` lists failures with `error` (`code`, `type`, `message`,
  `attempt`). Transient errors (DB locked/disconnected, timeouts) are retried; domain and
  validation errors fail immediately.
- Cancel: `POST /jobs/{id}/cancel`. A queued job is cancelled at once; a running job gets
  `cancel_requested` and rolls back at the next checkpoint. A cancelled or externally failed
  `investigate_decision` job shows its investigation as `failed` (`job_cancelled` / `job_failed`).
- Orphan blobs can appear when a transaction rolls back after the file was written. They are
  harmless (content-addressed); no GC is implemented yet.
- Do not add log lines that contain request bodies, cookies, tokens or uploaded content.

## 11. Throttling and reverse proxy

The in-process limits (section 2) are counted per API process. With `N` uvicorn workers the
effective limit is `N x` the configured value, and counters reset on restart. For a shared limit,
add one at the reverse proxy, e.g. nginx:

```nginx
limit_req_zone $binary_remote_addr zone=jettae_auth:10m rate=30r/m;
location ~ ^/api/v1/(auth/(login|signup)|public/) {
    limit_req zone=jettae_auth burst=10 nodelay;
    proxy_pass http://jettae_api;
}
```

`/auth/refresh` is deliberately not in that per-IP zone: it already needs a valid refresh
cookie and the session's CSRF token, and is limited per session by the API. Behind a proxy all
browsers may share one IP, so a per-IP refresh limit would let one client's failed logins sign
everyone else out of the UI.

The client IP seen by the API is the real one only when the proxy is listed in
`FORWARDED_ALLOW_IPS` (default `127.0.0.1`). This nginx snippet was not executed on the
development machine.

## 12. Migration 0002 (document content dedupe)

`0002` drops `UNIQUE(tenant_id, content_hash)` on `document_versions` and adds a plain index. A
correction upload that restores earlier content (v1=A, v2=B, v3=A) creates version 3; dedupe is
decided in code inside the tenant-serialised write transaction. Downgrading to `0001` fails if any
tenant already stores the same content twice.

## 13. Migrations 0003 and 0004

- `0003` adds `document_heads` (current applied version per document) and
  `document_versions.apply_state / issue_count`. The newest PARSED version of each existing
  document becomes current with state `legacy`.
- `0004` adds `llm_budget` and `llm_budget_entry` (shared LLM spending ledger). Downgrade drops them
  (the spending history is lost; export it first if it matters).

## 14. Migrations 0005 and 0006

- `0005_auth_sessions` adds `auth_sessions` (one row per browser login; refresh-token family,
  absolute expiry, revocation). Access tokens issued before this revision have no `sid` claim and
  are refused, so every user signs in once after the upgrade.
- `0006_investigations` adds `agent_investigations` (strategy, mode, status, findings, usage, error,
  job id, decision result hash). The worker writes the final state in the same transaction as the
  job's `succeeded` mark.
