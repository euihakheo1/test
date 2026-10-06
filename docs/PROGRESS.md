# PROGRESS (append-only)

## 2026-10-06 — FOUNDATION

### Done
- `pyproject.toml` (src layout, package `jettae`, Python `>=3.12,<3.13`, script
  `jettae = jettae.cli:app`, hatchling build). All planned runtime deps + extras
  `llm` (anthropic, openai), `agents` (langgraph), `postgres` (psycopg[binary]),
  `dev` (pytest, pytest-asyncio, hypothesis, ruff, mypy). `uv sync --all-extras` succeeded on
  Windows; nothing had to be dropped. `uv.lock` resolved 108 packages.
- `git init` (no commit), `.gitignore` (data/raw/, *.db, __pycache__, node_modules, .next, .env, caches).
- Pure packages:
  - `domain`: `Money` (int minor units, same-currency ops, `apply_rate` with `RoundingMode`
    FLOOR/HALF_UP, float rejected), date helpers (civil-law `add_days`, `month_end`,
    `next_business_day`, UTC/Asia-Seoul), canonical JSON hashing (rejects float), models
    (`DocumentVersion`, `SourceSpan`, `Fact`, `Invoice`, `SettlementLine`, `BankTxn`,
    `Agreement`, `Allocation`, `Computation`, `Decision`, `Approval`, `Change`, `RecomputePlan`),
    status enums (`ReconcileStatus`, `ReviewStatus`, `DocumentStatus`, `TradeType`, `LineKind`,
    `DocKind`, `ChangeKind`), errors.
  - `rules`: `RuleRegistry`/`RuleVersion` (effective/known dates, source URL, `verified` flag);
    `calendar_kr.KrCalendar` (holidays.KR + JSON/YAML override file
    `src/jettae/rules/kr_holiday_overrides.json`, empty by default); `kr_retail.compute_due`
    (direct 60d, consignment 40d, subcontract 60d, interest 15.5%/365, rollover variants,
    `Insufficient` for missing base date / trade type, tax-invoice date never substituted,
    2026 amendment registered INACTIVE with `effective_from=None`, monthly-settlement variant).
  - `recon`: candidates/normalisation, `AllocationBook` + `check_conservation`, matcher
    (reversals, reference incl. settlement-group netting, unique 1:1, bounded subset-sum with
    credits, partial, fee tolerance, caps -> AMBIGUOUS, counterparty conflict -> CONFLICT).
  - `evidence`: `Snapshot` (tenant-checked), `QueryScope` + result-set hashes,
    `DependencyGraph` (doc->fact->record->computation->decision + scopes, JSON round-trip),
    `invalidate.plan` (direct edges, scope re-evaluation, new/removed subjects,
    `fallback_full` on anything untracked), `engine.full_recompute` /
    `incremental_recompute` (group-level).
  - `verify`: citation, numbers-vs-engine, conservation, wording (no legal conclusions).
  - `app`: ports (Protocols), in-memory adapters, `JettaeService` (`register_document`,
    `record_facts`, `run_analysis`, `apply_change(verify_full=...)`, `approve` with
    expected_result_hash, `decision_view`, `list_required_documents`, `export_report` with
    validity re-check and CSV formula-injection guard), deterministic `render_explanation`.
  - `cli.py`: Typer root; lazy registration of ingest / sources (ftc, bpi2019, law) / eval /
    agents / mcp / api / worker; `jettae rules due`, `jettae rules list`, `jettae modules`.
- Tests (`tests/unit`, hand-written fixtures only): 64 tests incl. hypothesis properties
  (allocation conservation; incremental == full recompute incl. identical dependency graph).
- `docs/ADR/0001-architecture.md`.

### Rule sources checked (2026-10-06, 법제처 DRF API, `OC=test`)
- 대규모유통업법 (MST 288601, 시행 2026-10-02 현행본) 제8조: 특약매입 등 판매마감일부터 40일,
  직매입 상품수령일부터 60일 `<신설 2021.4.20>`. 직매입 version effective 2021-10-21 is taken from
  SPEC §4 (consistent with the 신설 date); consignment version start 2012-01-01 and
  하도급법 제13조 version start 2009-04-01 (from the "[전문개정 2009.4.1]" mark) are NOT verified
  against the amendment history (`verified=False` in the registry).
- 하도급법 (MST 288625) 제13조 제1항: 목적물 등의 수령일부터 60일 이내.
- 「상품판매대금 등 지연지급 시의 지연이율 고시」 제2021-13호, 시행 2021-10-21: 연 15.5%.
- 「선급금 등 지연지급 시의 지연이율 고시」 제2018-21호, 시행 2018-12-06: 연 15.5%.
- Earlier notice versions are not registered: delays whose period starts before those dates
  get `interest=None` + unresolved `interest_rule_version`.

### Commands run
```bash
export UV="<path to uv>"   # machine-specific, see AGENTS.md
export UV_PROJECT_ENVIRONMENT="$HOME/.cache/jettae/venv"
"$UV" python install 3.12          # downloaded 3.12.15; uv printed a minor-version-link error
"$UV" sync --all-extras            # OK (venv uses %USERPROFILE%\AppData\Local\Programs\Python\Python312, 3.12.0; not Anaconda)
"$UV" lock --check                 # OK
"$UV" run ruff check .             # All checks passed
"$UV" run ruff format --check .    # 41 files already formatted
"$UV" run mypy src                 # Success: no issues found in 30 source files
"$UV" run pytest -q                # 64 passed
"$UV" run jettae rules due --type direct --base 2025-08-07 --paid 2025-10-20 --amount 10000000
```
Extra check (scratch script, not committed): injecting a bug that freezes the
`agreements`/`bank_txns` scope hashes makes `test_incremental_equals_full` fail; over 150
random scenarios (344 change steps) 187 steps recomputed only a subset of groups, 76
recomputed nothing, 0 fell back to full.

### Not verified / open
- Holiday overrides: the override file is empty; no 임시공휴일 beyond what `holidays==0.106`
  knows has been verified. Add entries with a source URL when needed.
- Whether 근로자의 날 etc. count as non-business days is whatever `holidays.KR` says.
- Rollover and rounding are configuration/agreement conditions; the engine never decides them.
- `ingest_document` (listed in ARCHITECTURE) is not implemented here — it belongs to the ingest
  task; ingest should call `JettaeService.register_document` + `record_facts`/`apply_change`.
- No DB adapter yet: `app.ports` must be implemented by `jettae.db` (incl. `ResultStore` and a
  transactional `UnitOfWork`). Approval concurrency was tested only with the in-memory
  per-tenant lock (threads), not with SQLite/PostgreSQL.
- No real-data evaluation was run in this task (none required); no numbers are claimed.

### Contract notes for other tasks
- Decision ids are `dec:<subject_id>`; `Decision.result_hash` includes `inputs_hash`.
- `Approval(decision_id, result_hash, snapshot_hash, approved_by, approved_at, tenant_id, id)`
  — `tenant_id` added after the ARCHITECTURE field list.
- `RecomputePlan(affected, reasons, fallback_full, removed, notes)`; `.reason` is a summary string.
- `DueResult.interest` is `Money | None` (None when no interest notice version applies).
- CLI convention: sub-app modules expose `app: typer.Typer`; `jettae.mcp_server` /
  `jettae.api.cli` / `jettae.worker` may expose `app` or `serve`/`serve`/`run`.
  Sources mount under `jettae sources <ftc|bpi2019|law>`, evals under `jettae eval`.
- Tests: shared helpers live in `tests/unit/jt_unit_helpers.py` (imported by name; other test
  dirs should use distinct helper module names to avoid import clashes).

## 2026-10-06 — REALDATA (BPI 2019, law/notice sources, standard contracts, E4/E5/E6)

### Done
- `jettae.sources` (shared): repo/data paths (`JETTAE_DATA_DIR`, `JETTAE_REPO_ROOT`), manifest
  helpers, DRF OC redaction in URLs, `upsert_md_section` (idempotent `<!-- key:start/end -->`
  blocks in `docs/eval_results.md`; other tasks' blocks untouched).
- `sources/bpi2019`: 4TU downloader (DOI -> doi.org redirect -> article id -> `/v2/articles/{id}/files`
  -> `BPI_Challenge_2019.xes`; md5/size verified; maintenance detected from JSON
  `{"status":"maintenance"}`, HTML maintenance page or 503; exponential backoff; exit code 3 with
  the exact retry command; `--file` registers a local copy with sha256). Streaming XES reader
  (`lxml.iterparse`, clears processed traces; .xes/.gz/.zip). Conversion to one record per PO item
  (JSONL.gz, amounts kept as original strings). `p2p.analyze_item`: GR<->IR and IR (domain
  `Invoice`) <-> Clear Invoice (domain `BankTxn`) linking with `jettae.recon.reconcile` inside one PO
  item; equal amounts stay AMBIGUOUS (no FIFO guess); EUR cents via Decimal HALF_UP.
  CLI `jettae sources bpi2019 fetch|convert|stats`.
- `evals/bpi_eval.py` (E4): descriptive stats only (linking status counts, amount mismatches,
  payment days from IR / from GR with nearest-rank percentiles, items invoiced without clearing,
  processing time, per item category). `run` refuses any input that is not the registered real
  log (or its registered conversion). Korean statutory deadlines are not applied.
- `sources/law`: DRF client (OC from `JETTAE_DRF_OC`, default `test`), documents found by exact-name
  search (현행), article extraction via `JO=000800`/`001300`, notices via `target=admrul`; DRF
  rejections (`<Response><result>…`) recorded as failures. `check`: compares registry params with
  the fetched text and prints the evidence passage with the matched number marked.
  CLI `jettae sources law fetch|check|contract-fetch|contract-extract`.
- `sources/contract`: FTC board crawler (bordCd=204,key=205; titles with 직매입/특약매입/위수탁;
  format preference PDF > HWPX > HWP), in-repo OLE2/CFB reader + HWP 5.0 text extractor
  (raw-deflate sections, PARA_TEXT records, control chars), HWPX (zip+xml) and PDF readers,
  rule-based clause extraction (article, base-date phrase, period or blank "(  )일", spans,
  drafting notes "※" kept separate). `evals/contract_eval.py` (E5).
- `evals/recompute_eval.py` (E6): scenarios from real files only — `data/seeds/ftc_rows.csv`
  (columns mapped by alias incl. the FTC task's layout) and a deterministic BPI prefix when the real
  log exists. Base = receivables; changes = payments in paid-date order (+ correction rows if the
  file has any); after every change `incremental_recompute` (chained) is compared with
  `full_recompute` (decisions, groups, unattributed, dependency graph). `jettae eval recompute`.
- All three eval modules expose `run_cli` so `jettae eval bpi|contract|recompute` mount.
- Tests `tests/sources_misc` (36, hand-written fixtures only: tiny XES, hand-built OLE2/HWP5 and
  HWPX bytes, DRF XML snippets, board HTML, small CSVs). Includes a "teeth" test: a stale
  incremental result is reported as mismatching on every step.

### Real runs (2026-10-06)
- **law** `jettae sources law fetch` + `check`: all 4 documents fetched (대규모유통업법 MST 288601
  시행 2026-10-02 제8조; 지연이율 고시 제2021-13호 시행 2021-10-21; 하도급법 MST 288625 제13조;
  선급금 등 지연이율 고시 제2018-21호 시행 2018-12-06). Every check `match`: 직매입 60일, 특약매입 등
  40일, 하도급 60일, both rates 15.5%, notice effective dates/numbers equal registry versions,
  15.5% <= statutory cap 연 100분의 40. Inactive 2026 amendment versions: `info` (current text still
  states 60/40일). Output `data/results/law_check.json`, manifest `data/manifests/law.json`.
- **contract / E5** `jettae sources law contract-fetch` + `contract-extract`: 10/10 posts downloaded
  (all HWP 5.0 — the board offers no PDF/HWPX), 10/10 parsed, every form has a payment clause.
  Start-date wording equals the statutory base in 8/10; the two 면세점 직매입 forms say
  "상품 입고일부터 60일 이내" (reported as a wording difference to confirm, not a judgment).
  7/10 forms leave the period blank "(  )일" with a ※ drafting note stating the statutory days
  (60 or 40); 3/10 state the days (60, 60, 40). Coverage self-check: 17/17 paragraphs matching a
  broader keyword filter were extracted. No independent gold labels exist -> consistency only.
  Source typo seen verbatim in one 2024 form's note: "싱품수령일".
- **E6** `jettae eval recompute`: `ftc_rows` scenario on `data/seeds/ftc_rows.csv` (95 rows from the
  FTC task, 94 marked `verified=no`, 1 row skipped for an unparsable amount; 94 receivables in 48
  counterparty groups; 62 with a base date): 85 payment changes, **0 mismatching steps**, 0 full
  fallbacks; 303 of 7,990 decision evaluations recomputed (0.0379). The file has no correction
  rows, so no correction step was applied. BPI scenario skipped (log not available).
- **BPI / E4**: NOT RUN. `data.4tu.nl` answered `{"status": "maintenance"}` (API) and an HTML
  "offline for maintenance" page (dataset page) on every attempt: manual probes ~20:00Z and
  `jettae sources bpi2019 fetch --retries 4 --backoff 60` at 20:24–20:31Z (all 4 attempts in
  `data/manifests/bpi2019.json`). No BPI number is reported anywhere; E4 block in
  `docs/eval_results.md` says "Not run". The pipeline was exercised only on the hand-written test
  fixture `tests/sources_misc/fixtures/bpi_tiny.xes` (not real data, not in any result).
  Pending commands:
  ```bash
  uv run jettae sources bpi2019 fetch      # or: ... fetch --file <path/to/BPI_Challenge_2019.xes>
  uv run jettae sources bpi2019 convert
  uv run jettae sources bpi2019 stats      # E4 -> data/results/bpi_eval.json + docs/eval_results.md
  uv run jettae eval recompute             # E6 then also runs the BPI scenario
  ```

### Commands run
```bash
"$UV" run jettae sources law fetch; "$UV" run jettae sources law check
"$UV" run jettae sources law contract-fetch; "$UV" run jettae sources law contract-extract
"$UV" run jettae eval contract; "$UV" run jettae eval recompute; "$UV" run jettae eval bpi  # exit 3
"$UV" run jettae sources bpi2019 fetch --retries 4 --backoff 60      # exit 3 (maintenance)
"$UV" run ruff check <owned paths>; "$UV" run ruff format --check <owned paths>   # clean
"$UV" run mypy src     # no errors in owned files (errors remain in other tasks' files)
"$UV" run pytest -q    # 188 passed (36 in tests/sources_misc)
```

### Not verified / open
- E4 on the real 728 MB log (runtime, memory, actual linking rates) — pending 4TU. The streaming
  parser was tested only on the tiny fixture; real-scale memory behaviour is unmeasured. Very long
  traces (> 400 relevant events) skip event-level linking (counted in the report).
- The 4TU file-list JSON shape (`download_url`, `supplied_md5`) follows the djehuty v2 API as
  documented; it could not be observed live because of maintenance.
- E5 has no independent labels; extraction quality was checked only by the keyword-filter
  coverage self-check and by reading the 10 extracted clauses against the parsed text.
- HWP reader: distribution/password-protected HWP are refused (not decoded); not encountered here.
- E6 measures recompute equality on transcribed FTC rows; the rows themselves are unverified
  transcriptions (owned by the FTC task) — this does not affect the equality result but any other
  use of these numbers needs their verification.
- `docs/deps/realdata.txt`: no new dependencies.

## 2026-10-06 — INGESTION (`src/jettae/ingest/`, `tests/ingest/`)

### Done
- Parsers producing rows of `Cell(text, locator, raw)` (no float anywhere; xlsx floats → int/Decimal):
  - `csvx.py`: encoding BOM → strict UTF-8 → charset-normalizer (restricted to Korean/UTF codecs;
    EUC-KR decoded as CP949) → CP949; delimiter sniffing (`, \t ; |`) by record-width consistency;
    own RFC-4180 tokenizer recording `char_start/char_end` of each value in the decoded text
    (= document text), physical `line`, quoted newlines/doubled quotes.
  - `xlsx.py`: all sheets, merged ranges copied to every covered cell (`merged: "C3:E3"`), Excel
    date cells as dates, number-format zero padding (`00000` → leading zeros kept), 1904 epoch flag,
    formulas never evaluated (cached result only, `formula: True`, warning), hidden sheets flagged.
  - `htmlx.py`: HTML tables saved as `.xls` (common bank export); colspan/rowspan expanded.
  - `pdfx.py`: pdfplumber words → page text rebuilt by lines with char offsets; ruled tables (then
    text-strategy fallback) with integer bbox + char range per cell; page kinds text/image_only/blank;
    scanned → `UNSUPPORTED_SCAN` unless an `OcrProvider` (port in `ocrhook.py`; the implementation
    belongs to the OCR task) is passed; OCR failure → `FAILED` `ocr_failed`; mixed PDFs →
    `partial_scan` + `unread_pages`; encrypted → rejected; damaged → `CORRUPT` (never "no rows").
  - `detect.py`: type by magic bytes (PDF/zip-xlsx/OLE/HTML/text), `parse_bytes` / `safe_parse`
    (status + reason instead of exceptions). Legacy BIFF `.xls` → `FAILED` with a re-save hint.
- `table.py`: header-row detection against the synonym vocabulary (titles kept as `preamble`),
  two-row headers under merged group labels ("공급자" + "상호" → "공급자 상호"), 합계/총계/소계 rows
  separated (+ stated-vs-computed `TotalCheck`), repeated page headers skipped.
- `mapping.py`: deterministic header-synonym scoring → buckets `CONFIRMED/HIGH/MEDIUM/LOW/NONE`
  (exact / contains / partial; downgraded when <60% of sample values parse as the field type),
  greedy unique assignment, `confirm_mapping` (user: index or header), `MappingSuggester` protocol
  for an optional LLM (fills only unmapped fields, always LOW, always needs confirmation, suggester
  errors ignored).
- `formats/`: `hometax_etax_list` → `Invoice`, `kr_bank_txn` → `BankTxn`, `retail_settlement` →
  `SettlementLine`, `agreement_terms` → `Agreement`. Every value goes through `RowReader`, which
  creates a `Fact` with `SourceSpan(doc_version_id, locator + header, verbatim cell text)`.
  Base dates only from their own columns (상품수령일/입고일/하차일, 판매마감일); 작성일자, 지급일,
  정산일 are kept as facts only; `납품일` is deliberately not a 상품수령일 synonym. Missing trade
  type/base dates/counterparty/direction are listed in `missing`. 매입 invoices produce no
  receivable; direction from `self_brn`, an explicit option, or "매출"/"매입" in the title, else
  unknown (counterparty left empty, not guessed). Bank: 입금 − 출금 sign; both on one row → issue
  (no record); 취소/정정 text → `reversal_marker` fact (sign unchanged); counterparty only from an
  explicit column.
- `security.py`: caps (20 MiB, 100k rows, 200 cols, 300 pages, 50 sheets), xlsx zip checks (macro
  parts / macro-enabled content types rejected, path traversal, uncompressed size, compression
  ratio for members > 1 MiB, external links/embeddings warned), OLE VBA marker rejection,
  `escape_formula` / `strip_formula_prefix` (incl. full-width `＝＋－＠`) for exports (API task),
  `safe_filename`.
- `pipeline.py`: `analyze` (status without ids) → `build_records`; `ingest_document(service, ...)`
  registers the version (status + canonical text) and applies ADD/UPDATE/REMOVE changes through
  `service.apply_change` (record ids `<prefix>:<document_id>:<row-content-hash>#n`, so a corrected
  version updates kept rows and removes vanished rows/facts). Entry points for
  `jettae.db.ingest_bridge`: `parse_document(...)` and `suggest_mapping(...)`; a flat mapping
  `{field: col | header | None}` plus option keys `format_id, counterparty, self_brn, direction,
  account, accept_suggested`; the format is inferred only when exactly one format has all mapped
  keys or the declared document kind fits.
- `cli.py`: `jettae ingest inspect <file> [--json] [--format] [--map f=col]`,
  `jettae ingest parse <file> --out rows.json [--counterparty --self-brn --direction
  --accept-suggested --map]`; exit 0 parsed, 2 needs mapping, 3 scan, 4 failed/corrupt.
- Tests `tests/ingest` (85, hand-written in-memory fixtures only): cp949 KB-style CSV, UTF-8-SIG
  TSV, tokenizer property test (hypothesis, csv-writer round trip incl. offsets), xlsx with merged
  two-row header + serial dates + 합계 row, flat 홈택스 layout with repeated 상호 columns,
  settlement sheet base dates/leading zeros/formula cell, macro + zip-bomb + BIFF rejection,
  HTML-as-xls, text PDF table (hand-written PDF using the non-embedded CJK font
  HYSMyeongJo-Medium / UniKS-UCS2-H, because reportlab is not installed), Pillow-made scanned PDF
  (no OCR / fake OCR / failing OCR), mixed and blank PDFs, service integration (MATCHED decision on
  ingested data, missing base date, corrected version, citation check of every stored span), CLI,
  bridge.

### Sources for header vocabularies (checked 2026-10-06)
- 홈택스: official 손택스 list screen
  https://mob.tbet.hometax.go.kr/jsonAction.do?actionId=UTBETGBA01F001 (작성일자/발급일자/전송일자,
  종류(1)/(2) values, 발급유형 values, list columns 거래처상호·성명·공급가액·세액·작성일자·발급일자·
  전송일자·품목명·비고·승인번호); download procedure
  https://www.entax.co.kr/cs/help/content.php?k=lblhometaxdownload1 and
  http://www.os21.net/newjj/help/Guide.html.
- Banks (secondary sources only): https://www.lido.app/kr/eunhaeng-georaenaeyeok (KB and 신한
  column lists), https://www.semu.ai.kr/tools/bank-convert,
  https://mooders.co.kr/shinhan-transaction-statement/.

### Commands run
```bash
"$UV" run ruff check src/jettae/ingest tests/ingest        # All checks passed
"$UV" run ruff format --check src/jettae/ingest tests/ingest
"$UV" run mypy src/jettae/ingest                          # Success: no issues found in 19 source files
"$UV" run pytest -q tests/ingest                          # 85 passed
"$UV" run pytest -q                                       # 188 passed (whole repo at that moment)
"$UV" run jettae ingest inspect <hand-made 홈택스 xlsx>     # all 20 columns HIGH, exit 0
```

### Not verified / open
- The exact column labels/order of the PC-홈택스 Excel download were NOT verified (no public page
  lists them; a real file needs a Hometax login). Recognition is synonym-based and order-free;
  unknown layouts go through mapping confirmation. Whether the download is `.xls` (BIFF) or
  `.xlsx` was not verified either; BIFF `.xls` is not parsed (needs `xlrd`, not added).
- Bank layouts: only KB국민 and 신한 headers come from (secondary) public pages; 우리/하나/NH농협/IBK
  labels in the synonym list are unverified. No real bank export file was tested.
- Text-PDF table extraction was tested only on hand-written ruled tables; borderless real
  settlement PDFs rely on pdfplumber's text strategy and are untested.
- OCR: only the port and a fake provider are tested; the real provider belongs to the OCR task.
- `register_document` cannot update a stored version's status: after a user confirms a mapping for
  identical content, the version stays `NEEDS_MAPPING` although its records are created (needs a
  status-update port; outside my ownership).
- Through `parse_document`, record ids are keyed by `document_key` (defaults to the version id).
  The API should pass the document id as `document_key` to get UPDATE/REMOVE semantics across
  versions.
- Incident: at about 05:26 I ran `ruff check . --fix` on the whole repo instead of only my paths.
  It reported "2 fixed"; at least one was the import order of my `tests/ingest/test_ing_security.py`.
  The other may have been a safe autofix (import sorting / unused import) in another task's file.
  Files whose mtime matches that moment: `src/jettae/evals/recompute_eval.py`,
  `src/jettae/sources/ftc/collect.py`, `src/jettae/api/routes_jobs.py`,
  `src/jettae/api/routes_decisions.py` (their owners may equally have edited them then). Please
  review them with `git diff` after the first commit.

## 2026-10-06 — FTC (공정위 의결서 via 법제처 DRF; evaluations E1/E2/E3)

### Done
- `src/jettae/sources/ftc/`:
  - `client.py`: httpx DRF client (`lawSearch.do` JSON, `lawService.do` XML, `LSW/flDownload.do`
    images). OC from `JETTAE_DRF_OC`, default `test` (the DRF sample key, development only;
    production needs your own OC from open.law.go.kr). Minimum interval between requests (default
    1 s), exponential-backoff retries on transport errors/429/5xx, on-disk cache in
    `data/raw/ftc/{xml,img,q}` with `.m.json` metadata. Error payloads (`<Response><result>…`,
    HTML 500 for unknown ids) are rejected and never cached. URLs written to manifests use
    `OC={OC}`. Long Windows paths are opened with the extended-length prefix
    (`paths.long_path`): the repo resolves to a ~212-char MSIX-redirected path on this machine.
  - `parse.py`: sections (주문/이유/별지/결정요지 …), two footnote layouts (`<각주목록>` + `<각주>N</각주>`
    markers; inline footnotes spliced into paragraphs, heuristic, `confidence=low`), table images
    with flSeq and alt text, `<표 N>` caption assigned to the image after/before it, unit line
    `(단위: …)` or `[단위: …]`, continuation images, unlinked images (`table_image_14(1).png`,
    no flSeq), `delay_table_score`.
  - `extract.py`: case-level regex facts with provenance (decision id, section, char offsets;
    excerpt == `sections[section][start:end]`): supplier/transaction counts, delayed principal,
    total / unpaid interest (incl. "총 X원 중 Y원을 지급하지 아니"), delay-day ranges, legal vs other
    rates, statutory term + base ("상품수령일부터 60일"), base-date definitions, rounding and holiday
    notes. Masked numbers (`*`) -> `masked=true, value=null`. Low-confidence matches are flagged.
  - `collect.py` + `cli.py`: `jettae sources ftc fetch|images|facts|show`. Manifests
    `data/manifests/ftc.json`, `data/manifests/ftc_images.json`; facts
    `data/seeds/ftc_case_facts.jsonl`. A `--limit` run merges into the existing manifest/facts.
- Seeds (real data, transcribed by claude-agent, `verified=no`): `data/seeds/ftc_rows.csv`
  (95 rows from 8 images of 4 decisions: 19065 표 11/12/14, 19147 표 8/9, 19299 표 11, 16947
  표 5/6), `data/seeds/ftc_tables.csv` (per-table headers/semantics/printed totals),
  `data/seeds/ftc_rows.README.md` (exact images, conventions, uncertain readings). 쿠팡 19005
  표 56 was inspected: one aggregated line with masked counts, not transcribed.
- `src/jettae/evals/ftc_eval.py` (E1/E2/E3; results -> `data/results/ftc_eval.json`; block
  `<!-- ftc_eval:start/end -->` in `docs/eval_results.md`, replaced on re-run),
  `src/jettae/evals/cli.py` (`jettae eval ftc`; mounts `bpi_eval`/`contract_eval`/
  `recompute_eval` lazily — all three loaded at the last check), `src/jettae/evals/__init__.py`.
- Tests `tests/sources_ftc/` (51): parser and extractor on real DRF XML fixtures
  (`fixtures/f16947.xml` full; `x19065/x19005/x19299.xml` = 이유 trimmed; provenance in
  `fixtures/prov.json`), client (mock transport: cache, retries, rate limit, error payloads,
  offline mode, OC redaction), pipeline (fetch -> manifest/facts merge -> images), eval scoring
  logic on hand-written fixture rows.

### Commands run (Git Bash; ROOT/UV/UV_PROJECT_ENVIRONMENT as in AGENTS.md)
```bash
"$UV" run jettae sources ftc fetch --limit 3   # smoke: 3 ok, 13 network requests
"$UV" run jettae sources ftc fetch             # 311 decisions ok, 0 failed, 308 network requests
#   query '대규모유통' (사건명): total 111, selected 111
#   query '하도급대금 지연이자' (본문): total 797; title contains '하도급' -> first 200 in API order
"$UV" run jettae sources ftc images            # 1st run (older scorer): 237 ok, 0 failed, 23 unlinked, 146 decisions
"$UV" run jettae sources ftc images            # after scorer fix: 209 ok, 0 failed, 4 unlinked, 130 decisions (1 new download)
"$UV" run jettae sources ftc facts             # 2345 facts
"$UV" run jettae sources ftc fetch --limit 5   # from cache, 0 network; manifest kept 311 (merge)
"$UV" run jettae sources ftc fetch             # from cache, 0 network; refresh manifest
"$UV" run jettae eval ftc                      # final run 2026-10-05T20:41:08Z
"$UV" run ruff check / ruff format --check     # my paths: clean
"$UV" run mypy src/jettae/sources/ftc src/jettae/evals/ftc_eval.py src/jettae/evals/cli.py  # clean
"$UV" run pytest -q tests/sources_ftc          # 51 passed
"$UV" run pytest -q                            # 286 passed (last run; an earlier run had 2 failures in tests/api, another task's area)
```

### Results (from `data/results/ftc_eval.json`; full table in `docs/eval_results.md`)
- E2, 63 rows with a base date: due date, delay days and interest all match for 51/63 with
  rollover_off (floor or half_up), 46/63 with rollover_on, and 58/63 for at least one variant
  (40/45 on rows not marked UNCERTAIN). The matching convention differs per table: 16947 표 5
  needs rollover_on (its 각주 8: "마지막날이 공휴일인 경우 그 익일"), 19065 표 14 / 19147 표 9 match
  rollover_off, and 19299 표 11 has 2 rows that only match rollover_on.
- Range rows (19147 표 8): 4 computed; due dates of both endpoints match with rollover_on 4/4,
  rollover_off 2/4. 5 were not computed (see below).
- E2-cond (23 rows without a base date, recomputed from the table's own due date): delay days
  23/23; interest 8/23 (19065 표 12: 8/9; 19065 표 11: 0/14 — on every row the table's interest is
  lower than principal x 15.5% x days / 365; cause not investigated).
- E3: 23/23 rows without a base date abstained with required documents; 5/72 rows with a base date
  abstained because no rule version is registered (19147 표 8, goods received 2021-05..2021-09);
  0 other abstentions.
- E1: 16 text claims linked to transcribed tables. Text total = printed total 6/7; the 7th differs
  by 99 KRW, a floor/half-up difference the decision itself states (19065 footnote).
  Supplier/transaction counts vs 연번: 7/7 equal. Engine sums for the 3 complete tables: 16947 표 5
  rollover_on/half_up = 39,043,331 (text 39,043,331); 16947 표 6 rollover_on/half_up 2,281,893
  (text 2,281,895); 19299 표 11 rollover_on 3,569,558~3,569,561 (text 3,564천 원; principal is
  printed in 천 원). The other tables elide rows (⋮), so they were not computed.

### Not verified / open
- No person has checked the transcriptions. The 16947 images are low resolution (all rows marked
  UNCERTAIN), and the transcribed principal sum of 16947 표 5 differs from the printed total by
  700 KRW.
- Rules (not my files; for the rules owner): (a) 25 E2 rows have delay periods starting before
  2021-10-21. The registry has no 상품판매대금 지연이율 notice version for that time, so the engine
  returns no interest; the eval then uses the rate stated in the decision text (15.5%; e.g. 16947
  applies 15.5% to 2017–2021 delays) and reports it separately. (b) 19147 표 8 lists 60-day 직매입
  due dates for goods received 2021-05..09, before the registered effective date 2021-10-21, so
  the engine abstains. Both need checking against the amendment history (연혁) before anything is
  registered.
- The 200 하도급 decisions are the first 200 title-filtered body-search hits in API order. They
  are not ranked by relevance or date; no search sort parameter was verified for `target=ftc`.
- Inline-footnote detection and caption assignment are heuristics. They were checked on the
  fixtures and by looking at a few decisions by hand, not on all 311.
- The DRF terms of use / redistribution licence were not checked (the manifest says so). Images and
  XML are not committed (`data/raw/` is gitignored); the manifests carry sha256 + URL.
- No OCR was run; rows come only from manual (agent) transcription.
- Incident: I once ran `uv run` without `UV_PROJECT_ENVIRONMENT`, which created `jettae/.venv`
  (05:24); I deleted it right away. Another task's repo-wide `ruff --fix` touched
  `src/jettae/sources/ftc/collect.py` (see the INGESTION note); I re-checked it afterwards (lint,
  mypy, tests clean).

## 2026-10-06 — PERSISTENCE + API + WORKER + AUTH

### Done
- `src/jettae/db/`:
  - `orm.py`: SQLAlchemy 2 schema, using only generic types and `UtcDateTime`. Every table
    carries `tenant_id` (composite PK starting with `tenant_id`, or FK to `tenants`). The
    exception is `users`, a global identity reached through `memberships.tenant_id`.
  - Every domain table has `created_at`/`updated_at` and a payload `schema_version`.
  - Idempotency constraints:
    - `uq(tenant_id, content_hash)` on document versions;
    - `(tenant_id, entity, id)` PK for ledger records (the ingest-assigned external ids);
    - `(tenant_id, endpoint, key)` for Idempotency-Key.
  - `codec.py`: typed canonical-JSON codec driven by dataclass type hints. It uses no pickle and
    only an allow-list of types; decoded objects keep identical content hashes.
  - `repos.py`: implements every `app.ports` protocol:
    - Documents, Facts, Ledger, Decisions (current + append-only history, superseded),
      Approvals (append-only), `ResultStore` (result + dependency graph + config; returns
      `None` if the rule registry or calendar fingerprint changed), `UnitOfWork`;
    - plus column mappings.
  - `session.py`: ambient per-context session. A write transaction serialises per tenant
    (SQLite `BEGIN IMMEDIATE`; PostgreSQL `pg_advisory_xact_lock`) and is re-entrant, so a
    service call and the job completion commit atomically.
  - `jobs.py`: DB job queue.
    - Leases carry `lease_version`; heartbeats renew them.
    - Claims use `FOR UPDATE SKIP LOCKED` on PostgreSQL.
    - Transient failures retry with exponential backoff and jitter; a crash is resumed after
      the lease expires (and counted as an attempt).
    - Cancel: a queued job stops at once; a running job's transaction rolls back.
  - `plain.py` converts client JSON to and from domain objects (floats rejected; client
    `tenant_id` ignored). `runtime.py` wires everything, `migrate.py` wraps Alembic,
    `ingest_bridge.py` loads ingest lazily, `logs.py` writes JSON log lines, `config.py`
    holds the `JETTAE_*` settings.
- `src/jettae/store/files.py`: per-tenant content-addressed blob store.
  - The tenant directory is a hash of the tenant id; there is no cross-tenant dedupe.
  - Keys are validated (no path traversal); writes are atomic; reads re-verify the hash.
- `src/jettae/api/` (FastAPI, `/api/v1`):
  - auth: signup/login/refresh with rotation and reuse detection, logout, me, API tokens.
    Passwords use argon2id; JWT is HS256 with a 15-minute TTL; tokens are stored hashed.
    Roles: viewer < member < admin < owner. The tenant comes from the credential only.
  - documents: multipart upload (streaming size limit → 413; extension, declared type and
    magic bytes → 415; filename sanitising), list, versions, version detail, spans, content
    download, mapping GET/POST.
  - jobs: 202 with Location; status, list, cancel.
  - decisions: list with filters (status, review_status, subject_id) and cursor; detail with
    facts, spans, computations, variants, required documents, approvals and history.
  - approvals: 409 `stale_result` on hash mismatch. changes: JSON or file upload → job →
    impact plan.
  - reports: CSV (`csv_safe` on every cell, BOM) and HTML (escaped, CSP sandbox), with
    validity re-checked at export time.
  - health and ready (DB, Alembic head, blob directory writable).
  - Idempotency-Key on all POST create endpoints; common error schema; request-id and
    access-log middleware.
  - Routes only call `api/ops.py`, which calls `app.services`.
- `src/jettae/worker.py`: `jettae worker run [--once] [--concurrency N]`, a thread pool with
  heartbeats, handling `ingest_document`, `run_analysis` and `apply_change`.
  `jettae api serve | migrate | openapi` live in `api/cli.py`.
- `migrations/` (Alembic, `alembic.ini` at the repo root): revision `0001`, generic types,
  `render_as_batch`. File names are kept short because of Windows MAX_PATH.
- `deploy/`: Dockerfile, docker-compose (postgres:16 + migrate + api + worker), `.env.example`,
  `run_pg_tests.py`. `docs/runbook.md` covers environment variables, migrations, running,
  backup/restore, deploy and rollback.
- Tests in `tests/api` (55 = 52 run on SQLite + 3 PostgreSQL-only):
  - tenant isolation across API, DB, blobs and export (including forged and `alg=none`
    JWTs and client-sent tenant ids);
  - approval 409 and REVIEW_REQUIRED, with history kept;
  - idempotency: replay, key reuse → 422, per-tenant scope, upload content dedupe;
  - upload limits (413, 415, 422) and roles;
  - job lifecycle: run-once, cancel queued and running (rollback verified), transient retry,
    backoff timing, attempts exhausted, permanent and domain errors, crash resume after lease
    expiry (the stale worker cannot overwrite), heartbeat, bounded concurrency (max = 2);
  - migration upgrade → metadata diff = [] → downgrade base → upgrade; offline PostgreSQL DDL;
  - SQL adapters match the in-memory adapters (same hashes, explanations and history;
    incremental equals full through the SQL ResultStore with no fallback);
  - approval/change race on SQLite;
  - auth: rotation, reuse, expiry, API tokens;
  - CSV/HTML escaping;
  - integration with the real `jettae.ingest.pipeline` (cp949 bank CSV → PARSED; unknown
    layout → NEEDS_MAPPING → confirm → PARSED).

### Commands run
```bash
"$UV" run ruff check src/jettae/db src/jettae/api src/jettae/store src/jettae/worker.py tests/api migrations deploy   # All checks passed
"$UV" run ruff format --check (same paths)           # all formatted
"$UV" run mypy src/jettae/db src/jettae/api src/jettae/store src/jettae/worker.py   # no issues (32 files)
"$UV" run pytest -q tests/api                         # 52 passed, 3 skipped (PG-only)
"$UV" run pytest -q                                   # 287 passed, 3 skipped, 1 failed:
#   tests/ingest/test_ing_csv_property.py::test_roundtrip_values_and_offsets
#   (ingest task's area, still in progress in parallel; not touched by this task)
"$UV" run --with pgserver python deploy/run_pg_tests.py <tmp>/pgt
#   pgserver works on this Windows machine: PostgreSQL 16.2 (mingw64) -> test_api_postgres.py: 3 passed
#   (migrations up/down + metadata diff, API flow/isolation/409, concurrent SKIP LOCKED claims,
#    incremental == full through the PostgreSQL ResultStore)
"$UV" run jettae api migrate ; "$UV" run jettae api serve --port 8765   (real uvicorn process)
#   curl /health, /ready; signup -> POST /changes (202 + Location) -> "$UV" run jettae worker run --once
#   -> GET /decisions -> CSV export: OK
# SQLite online backup (sqlite3 backup API) + reopen restored copy: revision 0001, decisions readable
```

### Not verified / open
- **Docker was not run** (not installed). `deploy/Dockerfile` and `deploy/docker-compose.yml`
  were never built or started; only the compose YAML was parsed. `pg_dump`/`pg_restore`
  commands in the runbook were not executed.
- PostgreSQL was checked only through pgserver 16.2 on Windows, not on a Linux server or under
  multi-process load. The advisory-lock serialisation was exercised by the tests, but
  contention between several API processes and workers was not load-tested.
- On SQLite, `BEGIN IMMEDIATE` serialises all writers. While a long job holds the write lock,
  heartbeats wait up to `busy_timeout`; the lease length must exceed the longest job
  (default 60 s). PostgreSQL does not have this limitation.
- On this host, creating a fresh asyncio event loop per Starlette TestClient intermittently
  failed with `WinError 10014` (self-pipe socketpair). The tests share one session-scoped
  blocking portal; production code is unaffected.
- No orphan-blob GC; no login rate limiting or account lockout; no member-invite endpoint
  (one owner per signup; additional users need an endpoint later).
- Users are global (email unique) and memberships carry `tenant_id`; this is the only table
  without `tenant_id`.

### Contract notes for other tasks
- Ingest bridge: `JETTAE_INGEST_ENTRYPOINT` (default `jettae.ingest.pipeline`) must provide
  `parse_document(content, *, filename, media_type, kind, tenant_id, doc_version_id, mapping)`
  and `suggest_mapping(content, *, filename, media_type, kind)`. The ingest task already
  implements this, and it is tested in `tests/api/test_api_real_ingest.py`.
- MCP/frontend: use the HTTP API, or `Runtime.build()` + `JettaeService` in-process.
  Authenticate with `Authorization: Bearer jtk_...` API tokens. Treat `review_status` as
  computed (never stored).
- No foundation contract was changed. Adapter-only extras (not part of the ports):
  `SqlDocuments.find_by_hash/versions/page_latest/set_status/status_detail`,
  `SqlFacts.for_document`, `SqlDecisions.page_ids`, `SqlMappings`.

## 2026-10-06 — LLM + OCR(VLM) + AGENT FLOWS + MCP

Owned paths: `src/jettae/{llm,ocr,agents,mcp_server.py}`, `tests/llm_agents`.

### What was built
- `llm/`: one gateway (`llm/gateway.py`) in front of every provider.
  - Modes: `offline` (default) and `replay` answer only from the record/replay cache; a miss raises
    `ReplayMiss`. `live` needs `JETTAE_LLM_MODE=live` AND `JETTAE_LLM_BUDGET_KRW>0`.
  - Replay key (`LLMRequest.replay_key`): tenant, as_of, input hash (text + image sha256), model,
    prompt hash (system + schema + prompt version + max tokens), tool versions. Cache files live
    per tenant under `JETTAE_LLM_CACHE_DIR` (default `var/llm_cache`); they are tenant data.
  - Budget (`llm/budget.py`, `Decimal` KRW): the maximum cost (1 token per UTF-8 byte + per-image
    allowance + `max_output_tokens`) is reserved before each attempt and settled with the actual
    usage after. A failed attempt that may have been billed (timeout/5xx) keeps its reservation.
    Unknown model price -> refused unless `JETTAE_LLM_ALLOW_UNKNOWN_PRICE=1`. Built-in Anthropic
    USD list prices are converted only with an explicit `JETTAE_LLM_KRW_PER_USD` (no FX rate is
    assumed); `JETTAE_LLM_PRICES` sets KRW prices directly (OpenAI prices are not built in).
    Optional cumulative JSONL ledger: `JETTAE_LLM_LEDGER`.
  - Tenant consent: a request with `contains_tenant_data=True` goes to an external provider only
    when the tenant has `allow_external_llm=true`. Interim source: JSON file
    `JETTAE_TENANT_SETTINGS` (`{"<tenant>": {"allow_external_llm": true}}`); there is no DB
    column for it yet.
  - Structured JSON output validated with JSON Schema (one extra attempt on invalid output),
    limited retry with exponential backoff on transient errors (SDK retries disabled), timeout,
    token usage, run id, refusal / truncation reported as errors after settling the cost.
  - Providers: `anthropic.py` (Messages API, `output_config.format=json_schema`, base64 image
    blocks, model `JETTAE_LLM_MODEL`, default `claude-opus-5-5`), `openai.py` (Chat Completions,
    strict `json_schema`, model `JETTAE_OPENAI_MODEL`), `fake.py` (deterministic, in-process).
    Server-side refusal fallbacks are not enabled: a fallback model's price would not be covered by
    the reservation.
- `ocr/`: `base.py` (TableTranscriber, TranscribedTable; re-exports the ingest `OcrProvider`),
  `vlm.py` (`VlmTableTranscriber` for public table images, tenant `public`; `VlmPdfOcr` renders
  scanned pages with pypdfium2 and implements the ingest `OcrProvider`; `pdf_ocr_from_env` returns
  it only with `JETTAE_OCR_PROVIDER=vlm`), `manual.py` (cells CSV write/load, `ftc_rows.csv`
  loader, `ManualTranscriber` with image-hash check, cell comparison), `cli.py`.
  The FTC CLI has no OCR hook, so the separate command `jettae ocr ftc-tables` was added: it reads
  `data/manifests/ftc_images.json`, checks each image's sha256, transcribes through the gateway and
  writes `data/raw/ftc/ocr/vlm_cells.csv` (`verified=no`; never touches `data/seeds`).
  `jettae ocr compare A B` compares two cells CSVs.
- `agents/`:
  - `tools.py`: 10 typed tools: search_documents, get_source_span, list_transaction_candidates,
    reconcile_transactions, calculate_due, get_agreement_conditions, validate_evidence,
    propose_recompute, get_run_status and propose_missing_evidence.
    - Everything is read through `JettaeService` and its ports; dry runs use the pure engine.
    - Tenant comes from the context only; unknown args (e.g. `tenant_id`) are ignored.
    - Document text, excerpts and memos are returned under `untrusted_*` keys.
    - `as_of` = reference date for dry runs and known-time cut-off for documents/facts.
    - No tool approves or sends. `propose_*` only store DRAFT proposals (`sent: false`) in
      `AgentStore` (`JETTAE_AGENT_DIR`, default `var/agent`).
  - `planners.py`: `LLMPlanner` makes one structured call per step. The action schema's tool enum
    is limited to the role's tools. Tool results go in as JSON data (`OBSERVATIONS`), and a fixed
    system prompt says data is never instructions. Only a short action summary is requested; no
    reasoning is stored. `HeuristicPlanner` is deterministic and uses no LLM.
  - `flows.py`: `run_agent(strategy=single|roles)` uses the same tools, planner and limits for both
    strategies. `roles` runs extractor -> investigator -> verifier under a code supervisor.
    - Caps: steps, tool calls, LLM tokens, wall time and per-role steps.
    - Runs on LangGraph `StateGraph` when installed, else a deterministic fallback orchestrator
      with the same node functions; a test checks the step sequences are identical.
    - Final engine numbers come from code (`finalize` calls calculate_due /
      reconcile_transactions / validate_evidence), never from the LLM.
    - LLM summaries are checked for judgement wording and for numbers the engine did not produce.
    - Replay miss or refused live call -> `BLOCKED` with the engine numbers still reported.
  - `cli.py`: `jettae agent run --strategy single|roles --decision <id> --mode replay|live
    [--planner llm|heuristic] [--token jtk_... | JETTAE_API_TOKEN] [--as-of] [--json]`.
- `mcp_server.py`: official `mcp` SDK 2.3 `MCPServer` (the renamed FastMCP) exposes the same
  10 tools.
  - Auth is an API token, re-validated on every call: env `JETTAE_API_TOKEN` for stdio, or a
    per-request Bearer token for streamable-http via `ApiTokenVerifier` + `AuthSettings`.
  - The tenant is resolved server-side; a `tenant_id` argument is ignored. Every tool takes
    `as_of`. `propose_*` need role >= member.
  - Command: `jettae mcp serve [--transport stdio|streamable-http]`.

### Commands run
```bash
"$UV" run ruff format src/jettae/llm src/jettae/ocr src/jettae/agents src/jettae/mcp_server.py tests/llm_agents
"$UV" run ruff check  (same paths)          # All checks passed
"$UV" run mypy src/jettae/llm src/jettae/ocr src/jettae/agents src/jettae/mcp_server.py   # no issues (21 files)
"$UV" run pytest -q tests/llm_agents        # 60 passed
"$UV" run pytest -q                         # 348 passed, 3 skipped (second run; see flake note)
# real processes:
"$UV" run jettae mcp serve --transport streamable-http --port 8799   (temp SQLite DB, 2 tenants)
#   Bearer token A: calculate_due dec:I1 ok (tenant_id arg ignored); dec:BI1 (tenant B) -> not_found;
#   bad token / no token -> HTTP 401
# stdio server subprocess is exercised by tests/llm_agents/test_la_cli.py::test_mcp_stdio_server_process
"$UV" run jettae agent run --strategy roles --decision dec:I2 --planner heuristic --token <jtk>   # exit 0
"$UV" run jettae agent run --strategy single --decision dec:I1 --mode replay --token <jtk>        # BLOCKED, exit 2
"$UV" run jettae ocr ftc-tables --flseq 164088685 --flseq 163492277 --mode replay
#   real manifest + images read, sha256 checked; no recorded responses -> blocked=2, nothing sent, exit 2
```

### Not verified / open
- **No live (paid) LLM or VLM call was made**: no budget was provided. The Anthropic and OpenAI
  providers were tested only with stub clients (request shape, usage parsing, refusal mapping).
  There are no VLM transcriptions of the FTC images and no OCR accuracy figures. To record them:
  `JETTAE_LLM_MODE=live JETTAE_LLM_BUDGET_KRW=<n> JETTAE_LLM_KRW_PER_USD=<rate> ANTHROPIC_API_KEY=...
  "$UV" run jettae ocr ftc-tables --decision 19065 --limit 3`, then
  `"$UV" run jettae ocr compare <manual cells csv> data/raw/ftc/ocr/vlm_cells.csv`. The manual
  `ftc_rows.csv` uses normalised columns, so a cell-level comparison needs a cells-format manual
  transcription.
- No live agent run with a real model. Single vs roles quality (as opposed to identical engine
  numbers) was not measured.
- The streamable-http MCP transport was checked by hand once (above), not in the test suite.
- Root CLI registration: `jettae/cli.py` (foundation) mounts `jettae.agents.cli` as `agents` only.
  At import, `agents/cli.py` also mounts itself as `agent` and `jettae.ocr.cli` as `ocr` on the
  root app. The integrator should add `("jettae.agents.cli", "agent")` and
  `("jettae.ocr.cli", "ocr")` to `_SUBAPPS` and drop `_mount_aliases`.
- The API upload path does not pass an OCR provider yet (`db/ingest_bridge.py`, not owned here).
  Wiring: `jettae.ocr.pdf_ocr_from_env(tenant_id)` -> `ingest_document(..., ocr=...)`.
- `allow_external_llm` has no DB column (persistence task); it is read from `JETTAE_TENANT_SETTINGS`.
- `jsonschema`, `pypdfium2` and `pillow` are used but only installed transitively (docs/deps/llm-agents.txt).
- `ToolContext.config()` calls `JettaeService._today()` (private) for the default as_of.
- Flake observed once: the first full-suite run under heavy machine load (388 s) had 44 errors
  in `tests/api` fixture setup (`OSError: [WinError ...]` from the event-loop/socket issue noted
  by the API task). Those tests run before `tests/llm_agents`. A re-run passed (348 passed,
  3 skipped, 90 s), and `tests/api` alone passed (52 passed, 3 skipped).
- Process slip: one throw-away text-replacement script was run with the system `python` (not
  `uv run`) before switching to `"$UV" run python`; it only edited two source files of this task.

## 2026-10-06 — FRONTEND (Next.js, frontend/)

### Done
- `frontend/`: Next.js 16.3.8 App Router + TypeScript + ESLint, npm with `package-lock.json`. Korean UI. No Tailwind,
  no Google fonts (build works offline). Pages: `/login` (로그인·가입), `/upload` (drag-and-drop, type/size guide,
  document list), `/mapping?dv=` (suggested mapping table with confidence buckets + reasons, CSV preview, edit,
  counterparty/direction/self_brn options, confirm -> re-read job), `/analysis` (as_of / rollover / rounding,
  recent jobs), `/job?id=` (polling 1s -> 5s, cancel, per-type result), `/results` (status badges for all six
  ReconcileStatus values, amount / allocated / open difference, per-variant due date / delay days / interest,
  assumptions, unresolved conditions, required documents, filters, cursor paging), `/decision?id=` (facts with
  locator text: sheet/row/col or page/char range; CSV/TXT source fetched and the char range highlighted with an
  excerpt-mismatch warning; utf-8 and cp949 decoding; rules + source links; checks; history; approval with
  `expected_result_hash`, on 409 the latest result is reloaded and the change is explained), `/changes`
  (correction/additional upload; decisions snapshotted before upload, impact plan + 이전/현재 comparison after the
  job), `/report` (CSV/HTML export, `require_approved` 409 lists invalid ids), `/check` (public 무료 검산).
- One API client file `frontend/src/lib/api.ts`, typed against the live FastAPI app (routes and response shapes
  taken from `src/jettae/api` and checked against real responses). Base URL `NEXT_PUBLIC_API_BASE`; empty = same
  origin with a Next rewrite to `JETTAE_API_ORIGIN`. Idempotency-Key on create POSTs, refresh-once on 401, common
  error format parsed (`code`, `details`, `request_id`).
- UX rules: separate message styles for 처리 실패 / 자료 없음 / 판단 보류 (+ 안내/완료); scan/corrupt files are
  "처리 실패, not 거래 없음". Footer shows UI version, server API version (openapi `info.version`) and time zone;
  decisions show as_of, rule versions, result/snapshot hashes. Wording: `npm test` scans every UI source file and all
  label maps for the backend FORBIDDEN_PHRASES (+ "회수 가능", "법 위반"); the check caught one phrase on the home
  page, which was reworded.
- Pure helpers with unit tests (19 tests, `node --test`, hand-written fixtures only): formatting (integer won only),
  locator descriptions, span highlighting, cp949/utf-8 decode, mapping build (cleared suggestions are sent as null),
  decision key numbers (copied from engine outputs, never recomputed), before/after comparison, public-due response
  normalisation (accepts both `to_plain` and the CLI canonical `$type/$date/$money` form).

### Commands run (all from frontend/)
```bash
npx create-next-app@latest jettae/frontend --ts --eslint --app --src-dir --no-tailwind --import-alias "@/*" --use-npm --yes --disable-git --no-turbopack
npm run lint        # clean
npm run typecheck   # clean
npm test            # 19 passed
npm run build       # all 12 routes built (static, client-side data)
```
End-to-end check against the real backend (temporary, stopped afterwards; temp SQLite DB outside the repo):
`jettae api migrate`; `jettae api serve --port 8765`; `JETTAE_API_ORIGIN=http://127.0.0.1:8765 npm run build`;
`next start -p 3100`; `jettae worker run --once` between steps. Driven in a browser: signup -> drop-upload of a
hand-written settlement CSV (utf-8 BOM), a cp949 bank CSV and an unknown-layout CSV -> NEEDS_MAPPING shown ->
mapping page (suggestion preselected, preview) -> confirm -> job polling to 완료 -> analysis (as_of 2025-11-01)
-> results (MATCHED / UNMATCHED / INSUFFICIENT_EVIDENCE with required documents; both rollover variants) ->
detail (all highlighted spans equal their excerpts, utf-8 and cp949) -> result changed behind the page -> 확인
returned 409 -> page reloaded and explained -> 확인 succeeded -> corrected settlement as a new version via
/changes -> impact + 이전/현재 table (approved decision -> 재확인 필요) -> report export (409 list with
require_approved, CSV download otherwise) -> /check (404 shown as 처리 실패). The final `npm run build` was redone
with the default config.

### Not verified / open
- **Backend must add `POST /api/v1/public/due`** (no auth, rate-limited). Contract in `frontend/README.md` and
  `src/lib/api.ts` (`PublicDueRequest`; response = `compute_due` result as `to_plain` or CLI canonical JSON). Until
  then `/check` shows "처리 실패: 서버 미제공"; it does NOT compute in the browser (holiday calendar and rule
  versions live on the server). Result rendering was checked only by feeding the page the real
  `jettae rules due --json` output for the same inputs through an in-page fetch override.
- **Backend finding (not fixed, not my files):** uploading a corrected file as a new version of the same document
  through the API (`/changes/upload` with `document_id`) created new record ids (they contain the doc_version_id)
  and did NOT remove the rows of the superseded version. In the e2e run the v1 and v2 rows for A-1 both competed
  for the same payment and both became AMBIGUOUS. `jettae.worker.handle_ingest_document` / `_changes_for` only adds
  or updates; the superseded version's records need `remove` changes (the in-process `ingest_document` path is
  documented as handling this).
- The decision list API has no amounts/due fields, so `/results` loads each decision's detail (4 at a time, 50 per
  page) and `/changes` keeps details for at most 200 decisions before an upload. A summary field on
  `GET /decisions` would remove this N+1.
- `next start` rewrites are fixed at build time: `JETTAE_API_ORIGIN` must be set for `npm run build`.
- Screenshots did not render in the browser pane, so visual layout (including the 375px mobile width and dark
  mode) was not checked by eye; only DOM text/state was verified. No automated browser tests (Playwright not
  installed). The report download was triggered, but the saved file was not opened.
- npm audit: 5 high advisories in the dev-only eslint chain; the forced fix downgrades eslint-config-next and was
  not applied (see docs/deps/frontend.txt).

## 2026-10-06 — INTEGRATOR (review fixes, deps, CLI, docs, final status)

### Review findings applied
| sev | finding | fix | regression test |
|---|---|---|---|
| high | `recon/matcher.py` reference-pass subset with fee tolerance committed full open amounts -> `ConservationError` | shortfall `total - rem` written off via `_full_legs`; AMBIGUOUS if nothing can absorb it | `tests/unit/test_integ_recon.py` (exact repro + 400 random scenarios with tolerance) |
| high | `apply_change` reused the last run's `as_of` | `apply_change(..., as_of=None)` re-bases `as_of` to today (Asia/Seoul) unless a `config` change sets it; explanation shows "미지급분 계산 기준일(as_of)" | `tests/unit/test_integ_asof.py` |
| medium | approvals invalidated daily via `as_of` | `config` scope hash excludes `as_of`; new scope `as_of` recorded only by decisions with an open tranche; due inputs carry `as_of` only then | `test_paid_on_time_approval_survives_a_later_rerun` |
| medium | head assumption "as_of 미지정: 지연일수 0" next to real delays | head note dropped; per-tranche notes ("입금 [T1] 400,000원: 입금일 …까지", "미지급 600,000원: 기준일(as_of) …까지") | `test_assumptions_name_tranche_end_dates_not_unset_as_of` |
| medium | AMBIGUOUS items showed full unpaid interest | due date only; `max_delay_days`/`interest_total` = null, `delay_withheld="allocation"`; explanation/agent CLI/frontend show "미계산(입금 배분 미확정)" | `test_ambiguous_items_get_no_delay_or_interest` |
| medium | payments after `as_of` were allocated | `Snapshot` excludes `booked_date > as_of` (listed in `unattributed` with reason "기준일(as_of) 이후 입금…") | `test_payments_after_as_of_are_excluded` (+ incremental == full when only as_of moves) |
| low+medium | short BOM-less cp949 CSV decoded as UTF-16-BE (flaky property test) | UTF-16 only with BOM; order utf-8 -> strict cp949 -> charset-normalizer(cp949/euc_kr/johab) | `@example`s on the property test, `tests/ingest/test_ing_integ_text.py` |
| high | refresh-token rotation could fork on PostgreSQL | atomic `UPDATE … WHERE revoked_at IS NULL` + rowcount; loser revokes the family and gets `refresh_token_reused` | `tests/api/test_api_postgres.py::test_pg_concurrent_refresh_does_not_fork_the_token_family` (pgserver PG 16: passed), SQLite thread test |
| medium | correction restoring earlier content (A->B->A) was dropped | dedupe only against the latest version of the same document, inside the tenant write transaction; migration `0002` drops `UNIQUE(tenant_id, content_hash)` (plain index) | `test_correction_restoring_earlier_content_creates_new_version`; migration round-trip on SQLite + PG |
| medium | NUL in text: SQLite ok, PostgreSQL DataError | CSV/TXT: NUL anywhere -> `CorruptFile(code="binary")`; PDF words and stored document text: NUL -> U+FFFD 1:1 (`db_safe_text`) on both DBs | `test_nul_after_first_4k_is_rejected_as_binary`, API test on SQLite and on PG |
| low | login/signup unthrottled; argon2 under SQLite write lock | password verify in a read transaction; per-IP window (`JETTAE_AUTH_IP_PER_MINUTE`=30) on signup/login/refresh; per-email failure lockout (5 / 900 s); runbook §11 (proxy rule) | `test_failed_logins_are_throttled_per_email` |
| high | API correction upload added v2 rows beside v1 rows (both AMBIGUOUS) | bridge passes `document_key=doc.document_id` (and an OCR provider when `JETTAE_OCR_PROVIDER=vlm`); worker emits REMOVE for records/facts of older versions (ownership by span provenance) | `test_correction_upload_replaces_rows_of_superseded_version` (real ingest; A-1 keeps id, A-2 removed, approved decision -> REVIEW_REQUIRED, status MATCHED) |
| medium | BPI `--file` / E6 `--bpi/--ftc-rows` bypassed the real-data guard | `register_local_file` compares md5 with the 4TU list (or `--md5`), refuses mismatch, else `unverified_local` + warning in E4/E6 output; E6 ad-hoc inputs go only to `data/results/recompute_eval_adhoc.json` | `test_register_local_file_checks_published_md5`, `test_adhoc_input_never_touches_tracked_results` |
| medium | README / docs/ip / eval_protocol missing | `README.md` (Korean), `docs/eval_protocol.md`, `docs/ip/prior_art.md`, `docs/ip/disclosure_template.md` | — |
| low | `/check` called a missing endpoint | `POST /api/v1/public/due` (no auth, `JETTAE_PUBLIC_IP_PER_MINUTE`=30, StrictInt amount, nothing stored) | `test_public_due_without_login`, `test_public_due_is_rate_limited`; frontend normaliser checked on the real responses |
| low | reviewer re-run overwrote tracked eval outputs | `jettae eval ftc --out --md` | — |

Also: root CLI registers `agent` and `ocr` (old `agents` alias and `_mount_aliases` removed);
`jsonschema`, `pypdfium2`, `pillow` declared in `pyproject.toml` (re-locked, 108 packages);
`.gitignore` adds `var/` and the ad-hoc E6 file. Contract changes: `AnalysisConfig.fingerprint(include_as_of=)`,
scope kind `as_of`, `JettaeService.apply_change(as_of=)`, due outputs `max_delay_days`/`interest_total` may be
null with `delay_withheld`, `register_local_file(path, *, expected_md5, check_remote, client)`,
migration head `0002`.

### Final status
| area | implemented | verified (command) | not verified |
|---|---|---|---|
| domain/rules/recon/evidence/verify | yes | `uv run pytest -q` 371 passed, 5 skipped (PG-only); `uv run mypy src` clean; `uv run ruff check .` / `ruff format --check .` clean | — |
| ingest (CSV/XLSX/HTML-xls/PDF) | yes | tests/ingest; real cp949 bank CSV through API smoke run | real 홈택스/bank export files; borderless real PDFs |
| API + worker + auth | yes | tests/api; smoke: `jettae api migrate` -> `api serve` -> signup -> upload -> `worker run --once` -> run_analysis -> `/public/due` (temp SQLite) | multi-process load; nginx rule not executed |
| PostgreSQL | yes | `uv run --with pgserver python deploy/run_pg_tests.py <tmp>`: 5 passed (PG 16.2 mingw) | Linux PG server |
| frontend | yes | `npm run lint`, `typecheck`, `test` (19), `build` (12 routes) | visual layout by eye, browser e2e after these fixes (not re-driven) |
| E1/E2/E3 | yes | `uv run jettae eval ftc` re-run (numbers unchanged: 58/63 any variant, E3 23/23; only code sha changed) | transcriptions verified=no |
| E5 | yes | earlier run (unchanged code) | independent labels |
| E6 | yes | `uv run jettae eval recompute`: 85 changes, 0 mismatching, 0 fallback, 303/7990 | BPI scenario (no log) |
| E4 BPI 2019 | yes (pipeline) | fixture only | **not run**: data.4tu.nl still "Maintenance" at ~22:30Z (curl of the dataset page) |
| LLM/OCR/agents/MCP | yes | tests/llm_agents (stub providers, replay) | no live paid call (budget 0) |
| Docker | files only | — | not installed |
| docs/ip prior art | template + axes | — | patent texts not obtained: Google Patents answered the automated-query block page (HTTP 503); web search found no entry for the three numbers. Table cells left "미확인" |

### Commands run (Git Bash, AGENTS.md environment)
```bash
"$UV" lock; "$UV" sync --all-extras; "$UV" lock --check
"$UV" run ruff check . ; "$UV" run ruff format --check . ; "$UV" run mypy src
"$UV" run pytest -q                                   # 371 passed, 5 skipped
"$UV" run --with pgserver python deploy/run_pg_tests.py <tmp>/pgt   # 5 passed
"$UV" run jettae --help ; "$UV" run jettae modules     # 10 groups, all modules loaded
"$UV" run jettae eval recompute ; "$UV" run jettae eval ftc
(cd frontend && npm run lint && npm run typecheck && npm test && npm run build)
```

## 2026-10-06 — REVIEW FIX F6 (shared LLM budget) + F7 (E2 metric semantics) + hygiene

### F6 — LLM budget shared across objects and processes
- New `src/jettae/llm/budget_store.py`: `BudgetStore` protocol + `MemoryBudgetStore` (one process),
  `FileLockBudgetStore` (JSONL ledger, OS lock `msvcrt`/`fcntl.flock` on `<ledger>.lock` around
  read-check-append, fsync), `SqlBudgetStore` (tables `llm_budget`, `llm_budget_entry`; reservation =
  one transaction: SQLite `BEGIN IMMEDIATE` via the `jettae_sqlite_begin` execution option, PostgreSQL
  `SELECT … FOR UPDATE` on the budget row; amounts as integer 0.0001-KRW units so SUM is exact).
- `Budget` (`llm/budget.py`) is now a thin client of a store: every property reads the store
  (no cached totals); `reserve` = one atomic check of settled + in-flight (+ expired, + reconciled)
  against the limit. `Budget(limit, ledger_path=…)` keeps working (file store); `Budget(limit)` = memory.
- Crashed-process policy: reservations carry `expires_at` (TTL, default 900 s,
  `JETTAE_LLM_RESERVATION_TTL_S`); expiry frees nothing; `reconcile_expired()` turns them into
  "possibly billed" spending at the reserved amount; a late `settle` by the original process replaces
  it; double settle -> `RuntimeError`. Gateway: `maybe_billed` transient errors and unexpected
  (non-`LLMError`) exceptions settle at the full reservation; `LLMError` (4xx, rate limit) at 0.
- `gateway_from_env` -> `budget_from_env`: `JETTAE_LLM_BUDGET_DB` (+`JETTAE_LLM_BUDGET_ID`) -> SQL
  store; else `JETTAE_LLM_LEDGER`; else in live mode the file ledger `var/llm_budget.jsonl` (a live
  budget is never process-local); offline/replay -> memory. Older ledger lines (no `event`) count as spent.
- Tests `tests/llm_agents/test_la_budget_store.py`: two live objects racing 8+8 on 10 (file + SQLite,
  5 rounds); 12 objects x 2 rounds of 1 KRW on 10 -> exactly 10; two OS processes racing 8+8 on 10
  (file + SQLite, 3 rounds; the exited processes' reservation is still counted); TTL / reconcile /
  late settle; env selection; unexpected-failure billing. Mutation check: with the file lock replaced
  by a no-op (and a 50 ms read delay) the 8+8 race reserved twice (`['ok', 'ok']`).
- PostgreSQL: `test_postgres_objects_and_processes_share_one_limit` passed on pgserver PG 16
  (scratch script = `deploy/run_pg_tests.py` pattern running
  `pytest tests/llm_agents/test_la_budget_store.py -k postgres` with `JETTAE_TEST_PG_URL`).

### F7 — E2 metric semantics (`src/jettae/evals/`)
- `VariantCheck.all_ok` = all three checks evaluated AND all match (None never passes);
  `all_available_ok` = every evaluable check matches; plus `evaluated`, `fully_evaluated`, `fully_computed`.
- Summary keys (JSON): per variant `due_ok/delay_ok/interest_ok` (over evaluable rows),
  `all_three_ok` (over all E2 rows), `all_three_ok_of_fully_evaluable`, `all_available_ok`,
  `fully_evaluable`, `engine_full_computation`; per scope `abstained`, `engine_full_computation`,
  `evaluable_checks_per_row` (0..3), `evaluable_rows_per_check`; `post_hoc_any_variant`
  (label "제시한 계산 중 정답을 포함한 비율", post hoc) replaces `best_variant_any`; range rows
  `any` -> `post_hoc_any_variant`; row JSON `matched_all` -> `matched_all_three` + `matched_all_available`;
  `matched_variant_sets_by_table` -> `all_available_matched_variant_sets_by_table`.
- `ftc_eval.py` (1071 lines) split: `ftc_inputs.py` (197), `ftc_rows.py` (321), `ftc_aggregate.py` (386),
  `ftc_report.py` (191), `ftc_eval.py` (195: run/write + re-exports; `jettae.evals.ftc_eval.VariantCheck`
  and every name the old tests use still import from it). `code_hash` covers all five files.
- Re-ran `uv run jettae eval ftc` (2026-10-06T00:57:16Z, code sha `4470c161b628`); the ftc block of
  `docs/eval_results.md` and `data/results/ftc_eval.json` come from that run only. What changed in
  meaning: the old "all three, any variant 58/63" was "all available checks, post hoc". Same run: all
  three evaluated and matching 21/63 (rollover_off) and 17/63 (rollover_on) of all E2 rows (21/26 and
  17/26 of the 26 fully evaluable rows); post hoc all three 23/63; all available post hoc 58/63. Only
  26/63 rows have all three table values; the engine computed all three values for 38/63 (25 rows have
  no registered interest-notice version). Transcriptions stay `verified=no`. `docs/eval_protocol.md`
  E2 definitions rewritten.
- Tests `tests/evals/test_ev_ftc_metrics.py` (fixtures of the old file + metric unit tests + facade
  import paths + `jettae eval ftc --out/--md` on the real seed files).

### Hygiene
- `src/jettae/__init__.py`: no side effect on import (stdout reconfigure removed; ruff SIM105 gone).
  `src/jettae/cli.py`: `force_utf8_stdio()` (with `contextlib.suppress`) runs in `_CliApp.__call__`, i.e.
  only when the `jettae` console script (`jettae.cli:app`) or `main()` starts. Tests
  `tests/evals/test_ev_cli_stdio.py`. Checked: `uv run jettae --help > file` (Korean help, exit 0).
- README: Node.js 22.18+ (matches `frontend/package.json` engines); implemented-vs-verified table;
  Agent/MCP not wired into the web upload/analysis flow (only VLM OCR via `JETTAE_OCR_PROVIDER=vlm`);
  real LLM quality unverified; `as_of` = unpaid-as-of date + exclusion of later payments, not a full
  bitemporal restore; `known_at` only selects rule versions; contractual due date (약정 기한) is a
  separate date without the statutory rate (describes `rules/contract_term.py` as present in the
  workspace at 2026-10-06T01:00Z, written by another fixer); budget env vars.
- Replaced "other tasks" / "persistence task" wording in `llm/gateway.py` (`FileTenantPolicy`),
  `evals/__init__.py` and `evals/ftc_report.py` with interface/responsibility descriptions.

### Integrator notes (files I do not own)
- **`tests/sources_ftc/test_ftc_eval.py` now fails 2 tests** (`test_variant_matching`,
  `test_e2_summary`: `KeyError 'matched_all'` / `'all_ok'`). They assert the old, misleading semantics
  (`best_variant_any.all_ok.n == 3` counted rows whose due-date check was not evaluable). The file is
  superseded by `tests/evals/test_ev_ftc_metrics.py` (same fixtures, updated assertions): delete it, or
  replace its two tests with the versions in the new file.
- Migration for the budget tables (migrations/ is owned elsewhere): `SqlBudgetStore` creates
  `llm_budget(budget_id varchar(64) PK, created_at timestamptz)` and
  `llm_budget_entry(id varchar(64) PK, budget_id varchar(64) indexed, model varchar(200),
  purpose varchar(200), reserved_e4 bigint, cost_e4 bigint null, status varchar(16),
  created_at/expires_at timestamptz, settled_at timestamptz null, owner varchar(200) default '',
  note text default '')` lazily with `create_all(checkfirst)` from its own `MetaData`
  (`jettae.llm.budget_store.budget_tables()`), so `jettae.db.orm.metadata` and the alembic compare test
  are unaffected unless the store is used on that DB. To adopt: add a revision with these tables (or
  include `budget_tables()[0]` in the alembic target metadata) and construct
  `SqlBudgetStore(..., create_tables=False)`.
- Service deployments (worker with `JETTAE_OCR_PROVIDER=vlm`) should set
  `JETTAE_LLM_BUDGET_DB=$JETTAE_DATABASE_URL` so every worker shares one budget; add to `docs/runbook.md`.
- `docs/ARCHITECTURE.md` line 18 (`llm/ … budget.py`) could list `budget_store.py`.
- `pyproject.toml` `[project.scripts] jettae = "jettae.cli:app"` works as is (the app's `__call__`
  sets up stdio); no change needed.
- `rules/kr_retail.py` is part of the FTC eval code hash: re-run `uv run jettae eval ftc` after merging
  other fixers' rule changes so `docs/eval_results.md` matches the merged code.
- Full suite at the time of this entry: 431 passed, 6 skipped, 8 failed: the 2 above plus 6 in
  mapping/bridge tests (`test_api_flow::test_needs_mapping_then_confirm`,
  `test_api_real_ingest::test_unknown_layout_needs_mapping_then_confirmed`,
  `test_review_regressions::test_real_csv_invalid_row_diagnostic_survives_bridge`, `test_ing_bridge` x3)
  from in-progress F2/F3 work in files I do not own. `mypy src` errors were only in non-owned files
  (`app/doc_apply.py`, `ingest/pipeline.py`, ...).

### Commands run
```bash
"$UV" run pytest -q tests/llm_agents tests/evals          # all pass (PG test skipped without a server)
"$UV" run --with pgserver python <scratch>/pgbudget.py <tmp>/pgb   # 1 passed on PG 16
"$UV" run pytest -q tests/api/test_review_regressions.py -k "budget or all_three"   # 2 passed
"$UV" run pytest -q                                        # 431 passed, 6 skipped, 8 failed (see above)
"$UV" run ruff check . / ruff format --check   (owned files)  # clean
"$UV" run mypy src/jettae/llm src/jettae/evals src/jettae/cli.py src/jettae/__init__.py   # clean
"$UV" run jettae eval ftc                                  # regenerated the ftc block of docs/eval_results.md
"$UV" run jettae --help > file                             # exit 0, Korean help intact
```
Not verified: a real paid LLM call through the shared budget (budget 0); the POSIX `fcntl` branch of the
file lock (only the Windows `msvcrt` branch ran here); PostgreSQL on Linux.


## 2026-10-06 — REVIEW FIX F4 (document rows vs economic receivables) + contractual term

Scope: `src/jettae/domain/models.py`, `src/jettae/evidence/` (new `links.py`, `snapshot.py`,
`engine.py`, `invalidate.py`), `src/jettae/recon/candidates.py`, `src/jettae/rules/contract_term.py`
(new), `tests/unit/`.

### What changed
- **Model** (`domain/models.py`, existing names unchanged): `Receivable` (economic item;
  `Obligation` is an alias), `ReceivableBasis` (`settlement_line` | `invoice`), `DocumentRef`
  (document + `EvidenceRole` basis/corroborating/candidate + `LinkMethod` reference/user_confirmation),
  `ReceivableState` (`established` | `needs_confirmation`), `EvidenceLink` (user confirmation record:
  `same_sale` + `settlement_line_id`, or `separate_sale`), `ReceivableDocument = Invoice | SettlementLine`.
- **Linking rules** (`evidence/links.py`, per counterparty group, order-independent):
  every settlement line is a receivable (basis = settlement line). An invoice is corroborating
  evidence of a SALE line only by (a) same counterparty + same normalised reference matching exactly
  one SALE line, or (b) an `EvidenceLink`. Equal amount/date is never a link. An invoice becomes its
  own receivable (basis = invoice) only if the group has no SALE line or the user confirmed
  `separate_sale`. Otherwise (reference matches several lines, no reference match, conflicting or
  invalid confirmations) it is `needs_confirmation`: its own AMBIGUOUS decision with unresolved
  `evidence_link`, required documents, and `evidence.confirmation_required` (candidates + choices);
  it is not counted and gets no payment allocation. Linked documents with different amounts ->
  decision CONFLICT, unresolved `evidence_amount_conflict`, assumptions show both amounts, no `due`.
  Deduction/return/fee lines are never linked.
- **Snapshot**: `documents` (all rows), `economic_receivables` (id -> `Receivable`), `receivables`
  (decision subjects: receivable id -> basis document; a corroborating invoice is absent),
  `counted_total()`, `link_groups`, `receivable_model_groups`, new field `evidence_links`
  (entity `evidence_link`, tracked by the planner). Scope `receivables:<group>` hashes the group's
  documents, plus its links only when there are any (stored hashes of existing analyses stay valid);
  `snapshot_hash` likewise adds `evidence_links` only when non-empty. `as_of` / `known_at` comments now
  state the semantics (unpaid-as-of + payments after it excluded, no bitemporal replay; known_at =
  rule selection only).
- **Engine**: one decision per economic receivable (`dec:<basis id>`, so single-document receivables
  keep their ids). Every decision has a new computation `evidence` (`basis`, `basis_id`,
  `basis_amount`, `state`, `counted`, `documents` [basis first], `candidates`, `amount_conflict`,
  `confirmation_required`, `notes`). Reconciliation items are established receivables only; a payment
  quoting a corroborating document's reference (e.g. the invoice approval number) pays the basis
  (`recon.candidates.Item.alt_refs`). Corroborating documents' facts are in `facts_used` and graph edges.
- **Contractual term** (`rules/contract_term.py`): when an applicable agreement has
  `payment_term_days`, computation `contractual_due` = statutory base date for the trade type + N days
  (초일 불산입), no rollover, `interest = None` with `interest_note` (the statutory 15.5% is never
  applied), `statutory_comparison` (difference in days per statutory variant), `source` (agreement id,
  fact id, SourceSpan). Unresolved `contract_term_base` (the contract's counting start is not
  extracted); different term days across applicable agreements -> `conflict: true`, unresolved
  `contract_term_conflict`. Not computed for decisions already in CONFLICT.
- Review regression `test_one_sale_with_two_documents_is_not_two_receivables` passes with its input
  unchanged (receivables=1, decisions=1, balance=0).

### Tests (tests/unit)
- `test_receivables.py` (15): reference link, payment via invoice number, upload-order permutations
  with incremental == full, equal-amount documents never merged, invoice-only groups, multi-match,
  deduction lines, amount CONFLICT, user confirmation (link / separate / conflicting / other
  counterparty) with incremental invalidation, contractual due (span, no interest, comparison,
  conflict, missing base), `EvidenceLink` validation.
- `test_receivables_real.py` (4): real parser + `JettaeService` (in-memory repos): settlement CSV
  quoting the 홈택스 approval number + 매출세금계산서 CSV + bank CSV in both upload orders -> one
  decision, MATCHED, open 0, total 11,000,000; settlement with PO number + invoice -> invoice AMBIGUOUS
  with confirmation, total open 11,000,000 (not 22,000,000); ingested agreement CSV (지급기한 30) ->
  `contractual_due` 2025-09-06 citing the agreement document span.
- `test_recompute_property.py`: strategies now include settlement lines (sale/deduction, shared PO
  refs), invoices sharing those refs, duplicate-evidence invoices, `evidence_link` add/remove; new
  properties `test_duplicate_evidence_never_increases_receivables` and
  `test_upload_order_does_not_change_results`.

### Commands run
```bash
"$UV" run pytest -q tests/unit                      # 94 passed (incl. the 3 property tests)
"$UV" run pytest -q tests/api/test_review_regressions.py   # 9 passed (F4 probe input unchanged; the others were fixed by other tasks)
"$UV" run ruff check src/jettae/{domain,evidence,recon,rules} tests/unit ; "$UV" run ruff format --check ...  # clean
"$UV" run mypy src          # no errors in domain/evidence/recon/rules; errors only in app/ and ingest/ (other fixers' in-progress files)
"$UV" run pytest -q --continue-on-collection-errors   # final run: 2 failed, 446 passed, 6 skipped
```
The 2 remaining failures are `tests/sources_ftc/test_ftc_eval.py::test_variant_matching` and
`::test_e2_summary` (`KeyError: 'all_ok'`), from the F7 metric rework in `evals/ftc_eval.py` (not this
task). Earlier runs during the parallel work showed transient failures/errors in mapping, bridge,
upload and worker tests (`ImportError: MappingContractError`, Windows `OSError` while several fixers
ran pytest at once); they disappeared on re-run and none involved this task's files.

### Integrator notes (files this task does not own)
1. **Persistence of `EvidenceLink`** (db/, app/): `JettaeService.snapshot()` must pass
   `evidence_links=tuple(L.list(tenant_id, "evidence_link"))`; the ledger repo / ORM needs the entity
   `evidence_link` (columns: id, tenant_id, invoice_id, relation, settlement_line_id, confirmed_by,
   note, facts) and `db/codec.py KNOWN_TYPES` needs `EvidenceLink` (and `LinkRelation` is a StrEnum
   field). `_apply_one` already uses `entity_of(record)`, which returns `"evidence_link"`.
   A migration is needed; PostgreSQL-compatible plain columns suffice.
2. **Confirmation API** (api/, app/services): add a use case, e.g.
   `confirm_evidence_link(tenant, invoice_id, relation, settlement_line_id, expected_result_hash)`,
   that writes an `EvidenceLink` with `confirmed_by` = principal (server-decided) and calls
   `apply_change([Change(ADD|UPDATE, "evidence_link", id, link)])`. The decision to show is the
   AMBIGUOUS one with `unresolved` containing `evidence_link`; choices and candidate line ids are in
   `computation("evidence").outputs["confirmation_required"]`.
3. **Explanation labels** (`app/explain.py UNRESOLVED_LABEL`): add `evidence_link` ("같은 거래의
   정산 행과 세금계산서인지"), `evidence_amount_conflict` ("연결된 문서 간 금액 차이"),
   `contract_term_base` ("약정 지급기한의 기산점"), `contract_term_conflict` ("약정 간 지급기한 일수
   차이"). Optionally render `contractual_due` ("약정 기한 YYYY-MM-DD, 법정 기한과 N일 차이, 약정
   기한 기준 지연이자 미계산") and the `evidence` basis line; numbers used there are all engine values
   (verify.check_numbers passes).
4. **Totals in UI/report** (frontend `lib/view.ts`, api reports): a decision whose
   `computation("evidence").outputs.counted` is false is a document awaiting confirmation; show it as
   "확인 대기" and do not add its amount to receivable totals (its `recon.open` is 0 by design). Show
   `evidence.basis` / `documents` so the user sees which document the amount comes from.
5. `agents/tools.py list_transaction_candidates` keeps working: `snap.receivables[subject_id]` is still
   an `Invoice | SettlementLine` (the basis). For a corroborating invoice there is no decision.
6. Result hashes of all existing decisions change once (new `evidence` computation); stored approvals
   therefore show REVIEW_REQUIRED after the first re-analysis. Scope hashes are unchanged when no
   `EvidenceLink` exists, so the planner does not force a full recompute by itself.

### Known limits (not done)
- Two invoices of one counterparty with the same reference and no settlement line are still two
  receivables (e.g. the same 홈택스 file uploaded as two different documents). Merging them needs an
  invoice-to-invoice confirmation record; not implemented.
- A settlement line's base date / trade type is not filled from a linked invoice (basis-only values).
- The contractual counting start and any contractual interest rate are not extracted from agreements.
- In real 홈택스 data the invoice reference is the approval number, while settlement statements usually
  carry PO numbers, so most invoices in groups with settlement lines will be `needs_confirmation` until
  the user confirms (or the statement quotes the approval number). This is deliberate (no merge by
  amount/date) and needs the confirmation API (note 2) to be usable on the web.

## 2026-10-06 — Review fixes F1/F2/F3/F5: one document-application service

Scope: F1 (old version overwrites the ledger), F2 (column index vs. counterparty literal), F3 (row
issues / totals dropped), F5 (valid zero-row revision kept old rows). F4/F6/F7 belong to other fixers.

### What changed
- **`app/doc_apply.py` (new)** — `DocumentApplier`, the only code that turns a parse result into
  ledger changes. Used by the worker (`worker.handle_ingest_document` → `Runtime.applier`) and by the
  in-process path (`ingest.pipeline.ingest_document`, CLI/demo/tests). The duplicated diff logic
  (`worker._changes_for`, `pipeline._diff_changes/_persist/_PREFIX`) is deleted. Rules (the module
  docstring has the details): a version becomes current only if `version >= head.version`; the head
  is read, compared and written in the same tenant write transaction as the ledger change;
  non-PARSED results, PARSED-without-table, all-rows-excluded and "0 rows but 합계 ≠ 0" leave the
  ledger untouched; a recognised table with 0 rows and no errors removes the document's previous rows
  (`applied_empty`, `records_removed` reported); ownership is by provenance (fact spans of any version
  of the document + the records citing them, incl. span-less facts such as `counterparty_option`); an
  identical re-parse of the current version is `unchanged` (fingerprint) and writes nothing.
- **Partial application policy** — excluded rows, unreadable cells (`kind=value`) or a 합계 mismatch
  give state `applied_needs_ack`: readable rows are current, but `JettaeService.approve` raises
  `DocumentAckRequiredError` (HTTP 409 `document_ack_required`) for decisions whose `facts_used` cite
  that current version, until `POST /document-versions/{id}/acknowledge {fingerprint}` records an
  acknowledgment of that exact parse output (409 `stale_fingerprint` / `not_current_version` /
  `nothing_to_acknowledge` otherwise). A new parse output needs a new acknowledgment.
- **`app/contracts.py` (new, strict Pydantic)** — `MappingRequest {format_id?, columns: {field:
  int|null}, options: {counterparty_override?, account_override?, self_brn?, direction?,
  accept_suggested?}}` (JSON-strict: `"1"`/`true` rejected, one column per field, extra keys
  rejected); `ParseResult` (status, records, facts, issues, totals, `RowCounts`; PARSED requires
  counts; `applied_rows == len(records)`; a record with a non-text counterparty is rejected);
  `ApplyOutcome`, `IngestJobResult`. `MappingRequest.from_stored` reads mappings saved in the old flat
  shape unambiguously (int = column, also for counterparty/account; strings only for option keys;
  header-name columns make the job fail with `bad_mapping` "confirm the mapping again").
- **ingest/** — `IngestOptions.counterparty/account` renamed `counterparty_override/account_override`;
  `extract_table` counts `source_rows`, tags issues `excluded`/`value` and adds a generic issue when a
  builder drops a row silently; the duplicate date issue from `read_all_other` is gone;
  `parse_document` returns `ParseResult` (+ `observed_at`, so re-parses are deterministic);
  `options_from_mapping` accepts only `MappingRequest`; `jettae ingest parse --out` adds `counts` and
  the issue `kind`.
- **db/** — `DocumentHeadRow` (`document_heads`), `document_versions.apply_state/issue_count`,
  `SqlDocumentHeads`, `Repositories.heads` (memory + SQL); `DocumentRepo.versions` and
  `FactRepo.for_document` added to the ports; `ingest_bridge.ParsedDocument` is now `ParseResult`
  (issues/totals/counts propagate; contract violations raise `IngestContractError`, a permanent job
  failure). Migration **0003** creates the table/columns and backfills each document's newest PARSED
  version as current (`state='legacy'`, empty fingerprint, so its next parse is applied, never
  "unchanged").
- **api/** — `MappingConfirm.mapping: MappingRequest`; `POST /jobs` validates `params.mapping` with the
  same model and rejects unknown params; document outputs add `apply_state`, `issue_count`,
  `is_current`, `current_version`; `GET /documents/{id}/versions` adds `current` (the head); `GET
  .../mapping` returns the confirmed mapping in the new shape (`legacy_unreadable` when it cannot be
  converted); new `POST /document-versions/{id}/acknowledge`; error mapping for the new errors.
- **Job result / document detail** — the `ingest_document` job result is an `IngestJobResult`
  (document_id, version, counts, issues (at most 200) + issues_total, totals, notes, application,
  impact); `status_detail` stores the same counts/issues/totals/application.
- **frontend/** — `lib/mapping.ts` builds `{format_id, columns, options}` (a counterparty column is
  `columns.counterparty`; the typed name is `options.counterparty_override`; removing a suggested
  column sends `null`); mapping page: override/계좌 inputs, older-version notice, legacy-mapping
  notice, application panel; new `components/DocApplication.tsx` (반영/제외 행 수, 행별 문제 표,
  합계 확인 표, 빈 정정본 제거 건수, 이전 버전 안내, 제외 행 확인 버튼) used in the job view and on the
  mapping (document) page; the upload list shows the apply-state badge, issue count and current
  version; `lib/apply.ts` pure helpers with tests; new error texts.

### Test input changes (expectations unchanged)
- `tests/api/test_review_regressions.py::test_real_csv_invalid_row_diagnostic_survives_bridge`:
  mapping `{"counterparty": "가나유통", "trade_type": 1, ...}` became
  `{"columns": {"trade_type": 1, "reference": 2, "amount": 3, "goods_received_date": 4},
  "options": {"counterparty_override": "가나유통"}}`.
- `...::test_ui_counterparty_column_mapping_is_not_a_literal_value`: flat `{"counterparty": 1, ...}`
  became `{"columns": {"counterparty": 1, "trade_type": 2, "reference": 3, "amount": 4,
  "goods_received_date": 5}}`.
- Same contract update in `tests/api/test_api_flow.py`, `test_api_isolation.py`,
  `test_api_real_ingest.py` (header names became indexes) and `tests/ingest/test_ing_bridge.py`;
  `tests/ingest/*` use `IngestOptions(counterparty_override=...)`; `FakeIngest` (tests/api helper)
  returns `ParseResult` with row counts.

### New tests (real parser, worker, ledger unless noted)
`tests/integration/`: `test_int_versions.py` (v2 applied then v1 retried keeps the ledger and the
APPROVED state; v2 committing before v1 with two live worker threads; same version re-run is
unchanged; a crash before commit rolls back and the retry applies once), `test_int_mapping.py`
(counterparty column first/second/last, removed column, explicit override, 7 contract violations
give 422 on both endpoints, legacy stored mapping read as a column index, legacy header-name mapping
fails with `bad_mapping`), `test_int_issues.py` (amount error, date error, missing amount, 합계
mismatch, matching 합계, all rows excluded keeps v1, approval blocked until acknowledged,
stale/non-current acknowledgment), `test_int_zero_rows.py` (valid 0-row revision removes 2 rows and
their facts and reports it; corrupt file, table-less text, 0 rows with a non-zero 합계 keep data; the
next good revision still replaces), `test_int_inprocess.py` (in-process path: same rules, an xlsx
without tables is `not_applied_no_table`, approval guard; `FORMAT_IDS` equals the parser formats).
`tests/ingest/test_ing_bridge.py`: contract rejections, boundary checks, issues/totals/counts
through `normalize_result`. `tests/api/test_api_migrations.py`: 0002 to 0003 backfill + downgrade.

### Commands run
```bash
"$UV" run pytest -q                                   # 2 failed, 481 passed, 6 skipped
#   failures: tests/sources_ftc/test_ftc_eval.py::{test_variant_matching,test_e2_summary}
#   (KeyError 'all_ok' from the in-progress F7 rework of evals/ftc_eval.py, not this task)
"$UV" run pytest -q tests/api/test_review_regressions.py   # 9 passed
"$UV" run pytest -q tests/integration                 # 32 passed
"$UV" run ruff check src tests/integration tests/ingest tests/api/test_api_migrations.py   # clean
"$UV" run mypy src/jettae/worker.py src/jettae/app src/jettae/db src/jettae/api src/jettae/ingest  # clean
"$UV" run alembic -x url=sqlite:///$HOME/.cache/jettae/alembic_f1235.db upgrade head
#   then: current (0003), downgrade 0002, downgrade base, upgrade head, current (0003): all OK
cd frontend && npm run typecheck && npm test && npm run lint && npm run build   # all OK (21 tests)
```

### Not verified
- PostgreSQL: `tests/api/test_api_postgres.py` skipped (no server). Migration 0003 was rendered
  offline for PostgreSQL by `test_offline_sql_has_no_dialect_specific_types`; the backfill
  INSERT ... SELECT and head updates under `pg_advisory_xact_lock` were not run on a live server.
- The web pages were type-checked, linted and built, not clicked through in a browser.

### Integrator notes (files this task does not own)
1. `tests/api/test_review_regressions.py` has pre-existing ruff findings (I001 import order, four
   E501 print lines) that were left untouched (only the two mapping inputs were changed).
2. `docs/ARCHITECTURE.md` / README: document `app/contracts.py`, `app/doc_apply.py`, the
   `document_heads` pointer, the acknowledgment endpoint and the `columns`/`options` mapping contract.
3. The F4 notes above ask for `evidence_link` persistence and a confirmation API in app/, db/, api/;
   not done here (outside F1/F2/F3/F5).
4. Agents/MCP do not ingest documents; if they ever do, they must go through `DocumentApplier`.


## 2026-10-06 — INTEGRATOR (review fixes F1–F7 merged; integrator notes applied)

### Integrator notes applied
- **F7 leftovers**: deleted `tests/sources_ftc/test_ftc_eval.py`; `tests/evals/test_ev_ftc_metrics.py`
  is a superset (same fixtures, every old test kept with the corrected metric assertions, plus the
  new metric tests; checked with `diff --strip-trailing-cr`). Its docstring now says it replaced
  the old file.
- **Review regression file**: `tests/api/test_review_regressions.py` ruff findings fixed (import
  order, four long `print` lines split). Inputs/assertions unchanged by the integrator (the two
  mapping inputs were changed by the F1/F2/F3/F5 fixer, recorded above).
- **F4 persistence + confirmation API** (notes 1–5 of the F4 entry):
  - `app/ports.py`: `CONFIRMATION_ENTITIES = ("evidence_link",)`, `STORED_ENTITIES` = ledger +
    confirmations. Memory and SQL ledgers accept `evidence_link`; `db/repos.LEDGER_TYPES` and
    `db/codec.KNOWN_TYPES` include `EvidenceLink`. Stored in `ledger_records` (`entity='evidence_link'`,
    counterparty NULL): no schema change. `DocumentApplier` still only touches `LEDGER_ENTITIES`, so
    re-ingesting a document never deletes a user's confirmation (a link whose invoice is gone has no
    effect in `evidence/links.py`).
  - `JettaeService.snapshot()` passes `evidence_links`; `_apply_one` stores/removes them.
  - New use cases `confirm_evidence_link(tenant, decision_id, expected_result_hash, relation=…,
    confirmed_by=…, settlement_line_id=?, invoice_id=?, note=?)`, `withdraw_evidence_link`,
    `list_evidence_links`. Rules: optimistic concurrency on the decision's result hash (409
    `stale_result`); the invoice must be the decision's basis invoice or listed in its evidence
    documents/candidates; `same_sale` needs a SALE settlement line of the same normalised
    counterparty; one record per invoice (`elink:<invoice id>`, a new answer replaces the old one);
    `confirmed_by` comes from the credential. Violations raise `EvidenceLinkError` → 422 with codes
    `unknown_settlement_line`, `other_counterparty`, `not_a_sale_line`, `settlement_line_required`,
    `unexpected_settlement_line`, `invoice_required`, `invoice_not_in_decision`, `no_evidence_model`.
    Recompute is incremental (`apply_change`; the planner already tracks `evidence_link`).
  - API: `POST /decisions/{id}/evidence-link` (idempotency key supported), `GET /evidence-links`,
    `DELETE /evidence-links/{id}`. Decision list/detail summaries add `basis`, `basis_id`, `counted`.
    `POST /changes` still rejects `evidence_link` (a client cannot forge `confirmed_by`).
  - `app/explain.py`: labels for `evidence_link`, `evidence_amount_conflict`, `contract_term_base`,
    `contract_term_conflict`; explanation lines for the basis document / corroborating documents /
    "확인 대기" and for the contractual due date (약정 기한, difference to each statutory variant,
    "약정 기한 기준 지연이자 미계산"). All numbers are engine outputs (`check_explanation` passes).
  - Frontend: `lib/api.ts` (`DecisionSummary.basis/basis_id/counted`, `confirmEvidenceLink`,
    `evidenceLinks`, `withdrawEvidenceLink`), `lib/view.ts` (`evidenceInfo`, `contractInfo`),
    `lib/fmt.ts` labels; results list shows "확인 대기 · 합계 제외" and no open amount for
    `counted=false`; decision page has an evidence panel (basis / 보강 증빙 / 연결 후보, method),
    the "이 정산 행과 같은 거래 / 별개 거래" buttons, and a 약정 기한 block (no interest). No page
    sums open amounts across decisions, and the API/report do not either.
- **F6 budget migration**: `migrations/versions/0004_llm_budget.py` creates `llm_budget` and
  `llm_budget_entry` (+ index) exactly as `budget_tables()` defines them; it skips tables a store
  already created lazily (online mode). `jettae.db.migrate.target_metadata()` = ORM metadata +
  budget metadata; `migrations/env.py` and both metadata-diff tests (SQLite, PostgreSQL) use it.
  `SqlBudgetStore(..., create_tables=False)` works on a migrated DB (test). Runbook: service
  deployments set `JETTAE_LLM_BUDGET_DB=$JETTAE_DATABASE_URL`.
- **Docs**: `docs/ARCHITECTURE.md` (module list + "Contracts added by the review fixes": document
  application, mapping request, receivables/evidence links, contractual due, as_of/known_at, LLM
  budget); `docs/runbook.md` (budget env vars, `document_ack_required`, evidence-link endpoints,
  idempotent endpoints, section 13 on migrations 0003/0004); README (문서 행과 미수 채권 section,
  two rows in the implemented-vs-verified table). Remaining "other tasks" wording in
  `sources/__init__.py` and the ARCHITECTURE heading replaced.
- **FTC eval re-run after the merge**: `code_sha256` is still `4470c161b628` (no rule/eval file
  changed after the F7 run); `uv run jettae eval ftc` at 2026-10-06T01:51:29Z produced identical
  numbers (diff of `data/results/ftc_eval.json` without `generated_at`: empty); only the timestamp
  line of `docs/eval_results.md` changed.
- `deploy/run_pg_tests.py` now also runs the PostgreSQL budget test (`-k "pg_ or postgres"`).

### New tests
- `tests/integration/test_int_evidence_links.py` (real parser → worker → SQLite → API): settlement CSV
  with a PO number + 홈택스 매출 CSV + bank CSV in both upload orders → invoice decision AMBIGUOUS,
  `counted=false`, open total 0 (only the settlement line, paid by the bank row); stale hash 409,
  unknown line 422, bad relation 422, body `confirmed_by` rejected (422); `same_sale` → one decision,
  invoice corroborating via `user_confirmation`, MATCHED; full recompute from the DB gives the same
  single decision; withdraw → pending decision returns. `separate_sale` → two counted receivables, the
  single 11,000,000 payment is not guessed (both AMBIGUOUS, open 22,000,000). Another tenant gets 404.
- `tests/api/test_api_migrations.py::test_0004_budget_tables_are_migrated_and_shared`.
- `tests/api/test_api_postgres.py::test_pg_document_versions_and_evidence_links` (real parser on
  PostgreSQL: v2 then v1 retry keeps 2000 and head v2; evidence-link confirmation; full recompute).
- Frontend `lib.test.ts`: `evidenceInfo`, `contractInfo`.

### Final status (commands run, Git Bash, AGENTS.md environment)
```bash
"$UV" run pytest -q                                   # 477 passed, 7 skipped (PG-only) in 189 s
"$UV" run pytest -q tests/api/test_review_regressions.py   # 9 passed
"$UV" run ruff check .                                # All checks passed!
"$UV" run ruff format --check .                       # 231 files already formatted
"$UV" run mypy src                                    # Success: no issues found in 143 source files
"$UV" run alembic -x url=sqlite:///$HOME/.cache/jettae/integ/alembic_int.db upgrade head
#   -> 0001..0004; current 0004 (head); downgrade 0003; downgrade base; upgrade head; current 0004 (head)
"$UV" run --with pgserver python deploy/run_pg_tests.py $HOME/.cache/jettae/pgi
#   PostgreSQL 16.2 (mingw64): 7 passed, 16 deselected (test_api_postgres.py x6 incl. 0004 metadata
#   diff and the new review-fix test, budget objects+processes x1)
"$UV" run jettae eval ftc                             # same numbers, code sha 4470c161b628
cd frontend && npm run lint && npm run typecheck && npm test && npm run build   # all OK; 23 tests
```

### Not verified
- Pages were built and unit-tested, not clicked through in a browser (evidence panel, confirmation
  buttons, results badges).
- PostgreSQL on Linux; the POSIX `fcntl` branch of the budget file lock.
- Real 홈택스/정산서 originals: most real invoices quote the approval number while statements carry
  PO numbers, so they will wait for confirmation (by design; no amount/date merge).
- Known limits kept from the F4 entry: two invoices with the same reference and no settlement line
  are still two receivables; a linked invoice does not fill a settlement line's missing base date.


## 2026-10-06 — FINAL FIXER: second external review (12 findings)

All 12 findings (4 high, 5 medium, 3 low) are fixed, each with a regression test. Tests go through
the real parser (`ModuleIngest` -> `jettae.ingest.pipeline`), the real worker, Alembic-migrated
SQLite and the API unless marked "unit". No git commit was made.

### Status per finding

| # | Sev. | Finding | Fix | Regression test(s) |
|---|---|---|---|---|
| 1 | high | unrecognised sheet in a correction silently removed its rows; "모두 반영" claimed | `analyze` marks tables with data rows that no format recognises as *unread*; `RowCounts.tables_unread/unread_rows`; such a version is `applied_needs_ack` (0 recognised rows + an unread table = `not_applied_empty_unverified`); the applier carries the old rows over (see #3) | `tests/integration/test_int_review2.py::test_unrecognised_sheet_in_a_correction_keeps_that_sheets_rows`, `..._on_first_upload_is_reported`, `test_zero_rows_plus_unread_sheet_is_not_an_empty_revision` |
| 2 | high | one mapping applied to every table (columns swapped on a recognised sheet) | `MappingRequest.table`; `_mapping_targets`: a named mapping applies to that table (+ unrecognised tables with the identical header row); an unnamed one never to a confirmed-recognised table, and is rejected (`MappingContractError` -> job `bad_mapping`) when it cannot be attributed; a forced `format_id` is scoped the same way; the UI sends `suggestion.table` | `test_int_review2.py::test_mapping_for_one_sheet_never_rewrites_another_sheet`, `test_mapping_table_must_exist_and_unnamed_mapping_must_be_unambiguous`, `test_mapping_with_table_through_worker`; `lib.test.ts` "mapping: the suggestion's table is sent" |
| 3 | high | unreadable cell in a correction deleted the earlier receivable and orphaned its payment | `DocumentApplier` rule 7 (carry-over): a partially read version (excluded rows or unread tables) never REMOVEs owned records without a counterpart; reported as `records_carried_over/carried_over`; `approval_blockers` checks the document head (a carried record cites the old version); the acknowledgment removes them (`AckReport`, API `records_removed/removed`) | `test_int_review2.py::test_unreadable_cell_in_a_correction_keeps_the_receivable`, `test_complete_correction_still_removes_missing_rows`; PostgreSQL `test_pg_carry_over_and_duplicate_lines` |
| 4 | high | invoice of unknown direction counted as its own receivable (double count) | links rule 6: empty counterparty or `direction` missing -> `needs_confirmation`, `pending=invoice_direction`, status INSUFFICIENT_EVIDENCE, `missing` = direction/counterparty, own required documents, no confirmation choices (`direction_unknown` 422 on the link endpoint); the hometax builder adds a `direction` value issue (-> `applied_needs_ack`) | `tests/integration/test_int_review2_evidence.py::test_invoice_of_unknown_direction_is_not_a_receivable`; unit `tests/unit/test_review2_receivables.py::test_invoice_without_direction_is_pending_and_never_counted` |
| 5 | medium | the same settlement line in two documents counted twice | links rule 5: SALE lines with the same normalised reference (or settlement no. + amount) from an earlier *other* document version -> the later one is `needs_confirmation`, `pending=duplicate_line`; provenance `Snapshot.document_origins` (fact spans: version registration time, version id), hashed into the group scope when relevant; `EvidenceLink.document_entity="settlement_line"` + service `_confirm_duplicate_line` (`same_sale` = corroborating, `separate_sale` = counted) | `test_int_review2_evidence.py` (4 tests incl. full recompute == stored); unit tests incl. incremental == full after a provenance change |
| 6 | medium | confirmed mapping not reused for the next version | `worker._mapping_for`: job > this version's confirmed > the newest earlier version's confirmed mapping translated by header text (`inherited`); not translatable (or a removed column while the header row changed) -> `NEEDS_MAPPING`; `IngestJobResult.mapping` / `status_detail.mapping` (`MappingSource`) | `test_int_review2.py::test_confirmed_mapping_is_reused_for_the_next_version`, `test_inherited_mapping_follows_reordered_columns`, `test_mapping_that_no_longer_fits_requires_a_new_mapping` |
| 7 | medium | live budget ledger relative to the working directory | `default_ledger_path` (absolute per user: `JETTAE_STATE_DIR` > `%LOCALAPPDATA%\jettae` > `$XDG_STATE_HOME/jettae` > `~/.local/state/jettae`); a relative `JETTAE_LLM_LEDGER` is refused in live mode; the zero-budget live refusal happens before any store is opened; docstring, README, runbook corrected | `tests/llm_agents/test_la_review2_budget.py::test_two_runs_from_different_directories_share_one_limit` (3 env variants), `test_relative_ledger_paths_are_refused_in_live_mode` |
| 8 | medium | UI treated every approve 409 as a stale hash | pure `approveErrorView` (lib/apply.ts) branches on `code`; `document_ack_required` shows `errorText`, links each `details.documents` entry to `/mapping?dv=…`, does not reload or ask to press again | `lib.test.ts` "approveErrorView: stale result vs unacknowledged source document" |
| 9 | medium | E1 "engine" sums partly used the rate stated in the decision | `_engine_total` reports `interest_source`, `rows_registry_rate`, `rows_stated_rate`; summary `engine_computable_by_source`; markdown column "computed / 연번 (source)", per-row source label, summary "3 (1 registry rate; 2 … rate stated in the decision itself (partly circular))"; eval_protocol §E1 defines the metric | `tests/evals/test_ev_review2_report.py` (2 tests) |
| 10 | low | torn last ledger line bricked the budget | `FileLockBudgetStore._events`: under the lock an unterminated or unparseable *last* line is truncated (byte-exact); damage before it raises `BudgetStoreCorrupt(LLMError)` and the file is not rewritten | `test_la_review2_budget.py` (3 tests) |
| 11 | low | markdown cited the tracked JSON for `--out` runs | `render_markdown(rep, results_path=)`; `write_outputs` passes `display_path(out)` | `test_ev_review2_report.py::test_markdown_separates_stated_rate_sums_and_cites_its_json` |
| 12 | low | README called the cap a per-run budget | README table: cumulative cap over the whole ledger / DB budget row, not reset per run; how to start a fresh budget | (docs) |

### Contract changes (see docs/ARCHITECTURE.md "Contracts changed by the second review")
- `MappingRequest.table` (optional), `RowCounts.tables_unread/unread_rows` (default 0),
  `ApplyOutcome.records_carried_over/carried_over`, `MappingSource`, `IngestJobResult.mapping`,
  `DocumentHeadOut.records_removed/removed` (acknowledge response only),
  `DocumentApplier.acknowledge_report`.
- Domain: `PendingKind`, `Receivable.pending/missing`, `EvidenceLink.document_entity`;
  `build_group_receivables(..., origins=)`; `Snapshot.document_origins/duplicate_groups`. The
  evidence computation's `confirmation_required.kind` is now the pending kind (was always
  `evidence_link`); `choices` may be empty; `missing` is added when known.
- `jettae.llm`: `default_ledger_path`, `BudgetStoreCorrupt`; `DEFAULT_LEDGER` removed.
- `ftc_report.render_markdown(rep, results_path=None)`, `ftc_eval.display_path`.

### Test input / expectation changes (intent unchanged)
- `tests/api/test_review_regressions.py`: **no change** (9 passed before and after).
- `tests/integration/test_int_issues.py`, `test_int_zero_rows.py`: the exact `counts` dicts gained
  `"tables_unread": 0, "unread_rows": 0`.
- `tests/llm_agents/test_la_budget_store.py::test_budget_from_env_selects_a_shared_store` asserted
  the relative `var/llm_budget.jsonl` (the defect); it now passes `JETTAE_STATE_DIR` and asserts the
  absolute path.
- `frontend/src/lib/lib.test.ts` evidenceInfo test: `confirmation` gained `kind` and `missing`.

### Other changes
- Frontend: `DecisionView` (approve error view with document links; pending-kind texts; no
  confirmation buttons for `invoice_direction`; duplicate-line wording), `DocApplication`
  (carried-over notice, counts with unread tables, removed-on-acknowledgment count),
  `lib/mapping.ts` (`table`; ignores a confirmed mapping of another table), `lib/view.ts`
  (`pendingText`, confirmation `kind/missing`), `lib/fmt.ts` labels and the `direction_unknown` text.
- `app/explain.py`: labels/texts for `duplicate_line`, `invoice_direction`. API evidence-link output
  adds `document_entity`.
- `data/results/ftc_eval.json` and `docs/eval_results.md` regenerated: every number is identical to
  the previous run (JSON diff ignoring `generated_at`, `code_sha256` and the new source fields);
  code sha `4470c161b628` -> `6bcc83ac6bd6` (ftc_aggregate / ftc_report changed).

### Commands run (Git Bash, AGENTS.md environment)
```bash
"$UV" run pytest -q                                        # 507 passed, 8 skipped (PG-only) in 132 s
"$UV" run pytest -q tests/api/test_review_regressions.py   # 9 passed
"$UV" run ruff check .                                     # All checks passed!
"$UV" run ruff format --check .                            # 236 files already formatted
"$UV" run mypy src                                         # Success: no issues found in 143 source files
"$UV" run alembic -x url=sqlite:///$HOME/.cache/jettae/integ/alembic_r2.db upgrade head
#   -> 0004 (head); downgrade base; upgrade head; current 0004 (head)   (no new migration needed)
"$UV" run --with pgserver python deploy/run_pg_tests.py $HOME/.cache/jettae/pgr3
#   PostgreSQL 16.2 (mingw64): 8 passed, 16 deselected (incl. the new test_pg_carry_over_and_duplicate_lines)
#   A first run in .../pgr2 had 4 setup errors (WinError 10014 in socket.socketpair() of the test
#   client portal; environmental); the re-run passed.
"$UV" run jettae eval ftc                                  # numbers unchanged, see above
cd frontend && npm run lint && npm run typecheck && npm test && npm run build   # all OK; 27 tests
```

### Not verified
- Pages were built and unit-tested, not clicked through in a browser (approve error links,
  carried-over notice, duplicate-line confirmation buttons).
- Real 홈택스 exports and real re-issued settlement statements; the POSIX branch of the budget file
  lock; PostgreSQL on Linux.

### Known limits / notes for the integrator
1. One stored mapping per document version: a file with several tables that each need a manual
   mapping can be mapped one table at a time only (the next confirmation replaces the previous
   one). A per-table list in `MappingRequest` would lift this.
2. `GET /document-versions/{id}/mapping` shows only the mapping confirmed for that version; the UI
   does not yet show that a correction reused (`inherited`) an earlier version's mapping (the job
   result and `status_detail.mapping` do).
3. `EvidenceLink` gained the field `document_entity`, which is part of its content hash: groups that
   already have stored evidence links get a new receivables-scope hash once, so their decisions are
   recomputed and earlier approvals show as REVIEW_REQUIRED after the next analysis (history kept).
   No data migration is needed (the field defaults to `invoice`).
4. Duplicate detection needs provenance (facts with source spans); hand-entered records
   (`POST /changes`) are not compared.

## 2026-10-06 — Release integration: publication, demo, CI, audits (integrator)

Working tree only: the repository has **no commits** (no commit hash exists to report) and nothing
was pushed. All commands from the repository root in Git Bash on Windows, `UV_PROJECT_ENVIRONMENT`
on a short path outside the repo (see `AGENTS.md`).

### Integration of the build phase
- Already in place when this phase started: `migrate.target_metadata()` imports `orm_auth` and
  `orm_investigations`; `test_default_handlers_cover_all_job_types` allows the internal
  `investigate_decision` type; the root CLI callback loads `.env` and rejects unknown `JETTAE_ENV`;
  `jettae demo run`, the three root `.env.*.example` files and `scripts/secret_scan.py` existed.
- This phase: comment on `db/jobs.py JOB_TYPES` (why `investigate_decision` is absent); MCP docstring
  pointed at a non-existent `POST /auth/tokens` (now `/auth/api-tokens`); `secret_scan.py` OpenAI
  rule no longer double-reports `sk-ant-` keys; new `tests/config/test_secret_scan.py` (rules,
  placeholders, a secret committed then deleted is found in history and its value never printed).

### Publication
- `README.md` rewritten (Korean): prerequisites, install, `.env` loading rules (backend/frontend),
  local demo with three separate terminals (API / worker / web), own data and public data, Agent
  investigations + API-token CLI flow, real LLM section with every setting, prod deployment,
  tests/E2E/CI, implemented vs verified vs not verified. Option alternatives appear only in prose;
  every code block is runnable.
- `docs/runbook.md` rewritten for cookie sessions, CSRF, startup checks, migrations 0005/0006,
  `FORWARDED_ALLOW_IPS`, throwaway PostgreSQL. `docs/ARCHITECTURE.md` tree + "Contracts added for
  publication". `docs/SPEC.md` §9 alignment notes. `docs/eval_protocol.md` reproduction notes.
- `AGENTS.md`: no absolute paths (environment variables / relative paths only); fixtures allowed
  under `frontend/e2e/fixtures/`; publication and licence rules. Personal absolute paths in older
  entries of this file were replaced by `$HOME/` / `%USERPROFILE%\` (content otherwise unchanged).
- `deploy/docker-compose.yml`: `JETTAE_ALLOWED_ORIGINS` (required in prod) instead of
  `JETTAE_CORS_ORIGINS`, API published on host loopback only, LLM budget 0 by default;
  `deploy/.env.example` placeholders.
- New: `SECURITY.md` (GitHub private vulnerability reporting, no personal e-mail),
  `docs/PUBLIC_USE.md` (no licence chosen = all rights reserved; third-party data terms; user data
  never committed), `docs/security/dependency-audit.md`, `.github/workflows/ci.yml`.
- `LICENSE` not added; `pyproject.toml` licence text (`Proprietary`) unchanged.

### Commands and results (final run)
```bash
"$UV" sync --frozen --all-extras
JETTAE_LLM_MODE=offline JETTAE_LLM_BUDGET_KRW=0 "$UV" run pytest -q   # 614 passed, 8 skipped (PG-only)
"$UV" run ruff check . ; "$UV" run ruff format --check . ; "$UV" run mypy src   # clean; 152 source files
"$UV" run alembic heads                                               # 0006_investigations (head), single head
"$UV" run alembic -x url=sqlite:///<tmp>/al2.db upgrade head / downgrade base / upgrade head / check   # OK, no diff
"$UV" run --with pgserver python deploy/run_pg_tests.py <tmp>/pgfin   # PostgreSQL 16.2: 8 passed, 16 deselected
"$UV" run python scripts/secret_scan.py                               # 368 files, 0 commits, 0 findings
actionlint 1.7.12 (+ shellcheck 0.11.0) .github/workflows/ci.yml      # 0 errors
cd frontend && rm -rf .next node_modules .e2e-tmp next-env.d.ts tsconfig.tsbuildinfo
npm ci && npm run lint && npm run typecheck && npm test && npm run build   # OK; 40 unit tests
npm run e2e:install && npm run e2e                                    # 3 passed (chromium)
```
Dependency audit (details and decisions in `docs/security/dependency-audit.md`): pip-audit over
105 locked packages: 0 known; `npm audit --omit=dev`: 0; `npm audit`: 5 high, one advisory
(GHSA-vfj7-8cjw-p6xm, braces <=3.0.3, dev-only lint chain, no fixed release) deferred.

### Fresh-environment check (publish set only)
The files `git ls-files -co --exclude-standard` lists were copied to a new short directory with a
new virtualenv and followed as the README says: `uv sync --frozen --all-extras`, `cp .env.example
.env`, `jettae api migrate`, `jettae demo run --out-dir var/demo` (77 rows used, 18 skipped and
counted; 54 MATCHED, 23 INSUFFICIENT_EVIDENCE), API + worker + `npm ci/build/start`, browser login
with the demo account and the results page (only `jt_csrf` visible to scripts, browser storage
empty), the API-token CLI block (`agent run` single and roles, heuristic: 0 LLM calls), the runbook
curl session (403 without CSRF, logout 204 then 401), `eval ftc --out/--md`, `eval recompute`,
`rules due`, `ingest inspect/parse`. Full pytest there: first run 565 passed, 8 skipped, 47 setup
errors (error type not captured; the same files passed when re-run and a second full run gave
612 passed, 8 skipped). This machine has shown transient `WinError 10014` in `socket.socketpair()`
before; the cause of these 47 was not confirmed.
- Live LLM settings without editing `.env.live-llm.example`: capabilities report `live_enabled`;
  a live request handled by a worker without live settings ends `refused/live_disabled`; handled by
  a worker using the unedited file it ends `failed/llm_config` (placeholder KRW rate). Both: 0 LLM
  calls, cost 0.
- `JETTAE_ENV=production` exits 1; with `.env.prod.example` the API refuses (placeholder JWT secret),
  `sources ftc` refuses (OC), `demo run` refuses (not dev); the worker passes the gate and fails on
  the fake DB host with a long traceback (checked: it does not contain the example DB password).
- Runbook curl fix found here: a Korean literal in `curl -d` is mangled by Windows argument
  encoding (400 bad body); the runbook now uses an ASCII tenant name.

### Not verified
- The GitHub Actions workflow has not run on GitHub (no push); only actionlint/shellcheck. The
  Linux behaviour of the test suite, the PostgreSQL service container and Playwright `--with-deps`
  are therefore unconfirmed.
- Git history scan on this repository covers 0 commits (none exist); history scanning is tested only
  on a temporary repository.
- Docker/Compose, real HTTPS deployment behind a proxy (Secure cookies, Origin check through the
  proxy), Linux PostgreSQL server, paid LLM calls and LLM/Agent quality, the E4 BPI evaluation.
- Passing tests show the listed behaviours only; they do not establish production security or LLM
  accuracy.

## 2026-10-06 — public-release hygiene (first commits)

### Done
- Separation policy: `.gitignore` now also excludes `data/results/` (regenerable; reviewed
  summaries stay in `docs/eval_results.md`), `*.sqlite-journal`, blob/LLM cache/ledger names,
  coverage output, `frontend/` build and E2E artefacts, `docs/ip/sources/*.pdf`. Security and
  operations source code stays published. Policy table in `docs/PUBLIC_USE.md` §3; README,
  `docs/eval_protocol.md` and `docs/ip/disclosure_template.md` no longer call `data/results/` tracked.
- Personal-data scan of the `git add -A --dry-run` list (Windows/POSIX home-directory paths, the
  local account name, per-user application-data and agent scratch folders, e-mail, Korean phone and
  resident-number patterns, key formats): no personal path or account name; e-mail hits are
  `*.example`/`example.com` test addresses and one `db.internal` host in example DSNs; the
  application-data hits are `%LOCALAPPDATA%`/`%USERPROFILE%` references, plus three temp paths
  under `$HOME` in older entries of this file, replaced by `<tmp>/...`. The one phone-pattern hit
  is inside a wheel hash in `uv.lock`.
- gitleaks 8.30.1 (release zip, SHA-256 checked against the release checksum file):
  `gitleaks git --staged --redact` found 1 `generic-api-key` (the throwaway E2E sign-up password in
  `frontend/e2e/flow.spec.ts`); `.gitleaks.toml` allowlists exactly that rule + path + line shape.
  CI backend job now runs pinned gitleaks over every commit before installing anything.
- Repo-local git settings: `core.longpaths=true` (object writes failed with "Filename too long" in
  the long scratch path), identity `jettae-maintainer` with a GitHub noreply address.
- Commit, history scan and fresh-clone results: `docs/release-verification.md`.
- Commits `af18802` (initial) and `9cb638f` (gitleaks allowlist fix: `dir` mode on Windows uses `\`
  paths and CRLF). Fresh clone of `9cb638f` with a new virtualenv, README steps as written:
  `uv sync --frozen --all-extras`, `cp .env.example .env`, `jettae api migrate`, `pytest -q`
  614 passed / 8 skipped, `demo run` 54 MATCHED + 23 INSUFFICIENT_EVIDENCE, `npm ci`, lint,
  typecheck (no `.next`), 40 unit tests, build, `e2e:install`, E2E 3 passed; `git status` clean
  afterwards. gitleaks (history and exported tree) and `scripts/secret_scan.py`: 0 findings.
  Details and the not-verified list: `docs/release-verification.md`.

## 2026-10-07 — review fixes before publication (integrator)

### Done (each with a regression test)
- XLSX guard (`ingest/xlsx_guard.py`): parts are chosen like openpyxl (content types, workbook
  relationships, fixed styles/docProps paths, chart-sheet closure) instead of by root-element
  name; members are sniffed like libxml2/expat and XML is accepted only as UTF-8 or BOM-marked
  UTF-16 (`xml_encoding`); new caps for shared strings per string and in total, style records
  (`styles`, default 100 000), and elements of every other parsed part (`xml_elements`, 250 000
  per part / 750 000 total). Reviewer probes re-run: renamed `<sstX>`/`<workbookX>` roots and
  UTF-16LE without BOM are rejected; a 0.13 MiB styles bomb (200 000 `<xf>`) is rejected in 0.4 s
  instead of 4.5 s / 376 MiB. Tests: `tests/ingest/test_xlsx_limits.py` (11 new).
- Auth: `/auth/refresh` is no longer under the per-IP signup/login limiter; it is limited per
  session (`JETTAE_REFRESH_PER_MINUTE`). A rotated refresh token reused within
  `JETTAE_REFRESH_REUSE_GRACE_S` (20 s) in a valid session gets an access cookie only (overlapping
  tabs); later reuse still revokes the session. The web client clears the session only on
  401/403 from refresh (429/5xx/network -> state `error`). Compose sets `FORWARDED_ALLOW_IPS` to a
  pinned network gateway. Tests: `tests/api/test_auth_cookies.py` (6 new), updated reuse tests,
  `frontend/src/lib/client.test.ts` (4 new), E2E refresh test (grace 2 s in the E2E server).
- Config: prod live budget parsed as Decimal and must be finite, > 0 and <= 10 000 000 000 KRW
  (also enforced by `Budget`, `gateway_from_env`, `live_capability`); placeholder passwords in
  `JETTAE_DATABASE_URL` / `JETTAE_LLM_BUDGET_DB` are refused in prod (value never printed).
- `scripts/secret_scan.py`: path rules mirror every sensitive `.gitignore` entry (blob store, LLM
  cache/ledger, `data/results/`, prior-art PDFs, `logs/`, coverage, per-user tool settings); the
  inline `secret-scan: allow` marker is gone, replaced by `ALLOWLIST` (rule + path + line shape,
  also covering the same lines in the earlier commits). `.gitignore` adds `.npmrc`, `.pypirc`,
  `.netrc`, `.envrc`, `.claude/settings.local.json`, `CLAUDE.local.md`.
- CI: the PostgreSQL step uses `shell: bash` (pipefail) and requires a "passed" count; the
  frontend job asserts that no provider key is present. `tests/config/test_ci_workflow.py`
  checks both (and the Compose gateway) statically.
- `.gitattributes` (`* text=auto eol=lf`): seeds and engine sources hash the same on Windows;
  evaluation writers write LF. `uv run jettae eval ftc` re-run: all E1/E2/E3 numbers unchanged,
  engine code hash now that of the committed sources, input hashes the LF ones.
- `jettae eval contract` (and `sources law contract-extract`) exit 3 without touching
  `docs/eval_results.md` when a downloaded form is missing; the run header names the real command.
- Hermetic tests: `tests/_plugins/jettae_testenv.py` (pytest `-p`) removes `JETTAE_*` (except
  `JETTAE_TEST_PG_URL`), `ANTHROPIC_*`, `OPENAI_*` and ignores `JETTAE_ENV_FILE`/`./.env`.
- Docs: README (five example files incl. `deploy/.env.example`, per-component prod checks table,
  `modules` subcommand, proxy/refresh limits, hermetic tests, `eval contract` exit 3), runbook,
  ARCHITECTURE, PUBLIC_USE (seed file holds short verbatim decision excerpts; scanner coverage),
  `frontend/README.md`, dependency audit re-run.

### Commands run (main working tree, Windows 11, Git Bash)
- `uv run pytest -q`: 657 passed, 8 skipped (PostgreSQL-only).
- `uv run --with pgserver python deploy/run_pg_tests.py <tmp>`: 8 passed, 16 deselected
  (PostgreSQL 16.2, pgserver on Windows).
- `uv run ruff check .`, `uv run ruff format --check .` (268 files), `uv run mypy src` (152 files): clean.
- Alembic SQLite round trip (heads, upgrade, downgrade base, upgrade, check): no new operations.
- Frontend: `npm run lint`, `npm run typecheck`, `npm test` (44 passed), `npm run build`,
  `npm run e2e` (3 passed). One E2E run failed before the test update (immediate replay now hits
  the grace window); after that failure a `jettae worker run` process of that run was still alive
  and the next run's API exited at startup with WinError 10014; after stopping the leftover
  processes by hand the run passed (the causal link was not established).
- `actionlint` 1.7.12 with shellcheck on `ci.yml`: clean.
- `pip-audit` (PyPI and OSV) and `npm audit`: see `docs/security/dependency-audit.md`.

### Not verified
- GitHub Actions on GitHub, Docker/Compose (the gateway behaviour is reasoned from Docker's port
  publishing), a real HTTPS deployment, real browsers with several tabs, Linux PostgreSQL outside
  CI configuration, paid LLM calls and model quality, BPI 2019 (E4), real Hometax/bank exports,
  memory/time of the largest XLSX that passes every cap.

## 2026-10-06 UTC — independent final review and private vLLM runtime

Base GitHub commit: `874ca5c372ef160c1e0756495c34224b79283871`.
Verified code commit: `f7fceebbce7499c96b5a7520dabde28e0f451bda`.
Retained the completed security, parser, browser-session and Agent-web work. Added a private
text-only vLLM adapter, explicit local mode across CLI/API/worker/UI, zero external API charge
without opening the paid-provider gate, uncached local execution by default, SDK HTTP protocol
and web-worker regression tests. Added model revision/config examples, safe env generation,
WSL/local instructions, production web/Caddy/vLLM overlays, correct backend extras and Docker
context exclusions. Added the Docker build/config job; retained existing offline CI guards.
No LICENSE change, no real secret/user data/model weights committed, no GitHub push.

Direct commands/results (Linux, Python 3.12, uv 0.12.19, Node 24.19.0, npm 11.9.0):
- Fresh `git clone --no-local`, `uv sync --frozen --all-extras`, `npm ci`; initially no env files,
  DB, venv, node_modules or .next. `uv run jettae api migrate` and `uv run jettae demo run
  --out-dir var/demo` completed; generated private demo files stayed ignored.
- Fresh `uv run pytest -q`: **677 passed, 9 skipped, 1 warning**, 62.96s. Eight PostgreSQL-only
  skips and one Windows-only skip. `uv run ruff check .`, `uv run ruff format --check .`
  (275 files), `uv run mypy src` (153 files): clean.
- Fresh `npm run typecheck` (no generated .next before it), `npm run lint`, `npm test`
  (45 passed), `npm run build`: passed.
- SQLite Alembic heads/upgrade/downgrade base/upgrade/check completed, no new operations.
- Real API/worker/Next processes: login/public page/health/readiness 200; HttpOnly cookies,
  no body tokens, CSRF refusal 403, refresh 200, logout 204 and revoked session 401.
- `JETTAE_NEXT_STANDALONE=1 npm run build`, generated server.js + static runtime: login 200,
  nine referenced static files 200. Docker itself was not run.
- `uv run python scripts/secret_scan.py`: code-commit publish set 383, five commits/450 blobs,
  findings 0; gitleaks 8.30.1 `dir` and `git` with redaction: no leaks found.
- pip-audit 2.10.1 (PyPI and OSV): 103 environment-applicable packages, no known vulnerabilities.
  Initial tool download timeout recovered with a local tool-wheel directory. npm production
  audit 0; full audit five high, unchanged dev-only braces chain. See dependency-audit.md.

Not verified: PostgreSQL execution (pgserver blocked on OS user creation/transition), browser
E2E (Chromium CDN download was HTML, three launch failures), GPU model execution/LLM quality,
Docker/Compose/HTTPS, actual bank/Hometax exports, BPI 2019, transcript truth and peak/long-run
resource use. See FINAL_VERIFICATION.md for exact commands, boundaries and run instructions.
