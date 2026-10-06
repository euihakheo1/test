# ADR 0001 — Modular monolith with a pure core, group-level incremental recompute

- Status: accepted (foundation task, 2026-10-06)
- Context: SPEC v2 (2026-10-06), docs/ARCHITECTURE.md

## Decision

1. **Layering.** `domain`, `rules`, `recon`, `evidence`, `verify` are pure Python (stdlib +
   `holidays`; no FastAPI / SQLAlchemy / LangGraph / MCP / LLM SDK). `app` holds use cases
   against `Protocol` ports (`app/ports.py`) and ships in-memory adapters (`app/memory.py`).
   DB, API, MCP, worker, ingest, sources and evals are adapters/interfaces on top.
2. **Money and time.** `Money(amount:int, currency)` in minor units; rate math is `Decimal`
   with an explicit `RoundingMode` (`floor` | `half_up`); `float` is rejected by `Money` and by
   the canonical hasher. Business dates are `date`; system time is UTC-aware `datetime`.
3. **Rules are data with provenance.** `RuleVersion(rule_id, version, effective_from,
   effective_to, known_from, source_url, params)`; `effective_from=None` = registered but
   inactive (2026 amendment: direct 35 days / monthly 20 days after month-end / consignment
   20 days). `resolve(rule, on=base_date, known_at=...)` picks the latest active version.
   Article texts and both 15.5% notices were checked against the 법제처 DRF API on
   2026-10-06; unverified fields are flagged per version (`verified`, `source_note`).
4. **Due-date semantics.** due = base + N days (민법 제157조, initial day excluded). If the
   due date is a Saturday/Sunday/public holiday and `rollover` is not fixed by config or an
   agreement, both variants are returned with `unresolved=("rollover",)`. A missing base
   date returns `Insufficient(missing, required_documents)`; a tax-invoice date is never used.
   Interest = principal × 15.5% × delay_days / 365, rounded once per tranche
   (each payment allocation is its own tranche ending at its payment date; the open
   remainder ends at `as_of`).
5. **Reconciliation is deterministic and conservative.** Passes: reversals → reference
   (incl. settlement-group netting of deduction lines) → unique 1:1 amount match →
   bounded subset-sum (credits allowed) → single-candidate partial; repeat. Any tie,
   multiple solutions, or exhausted candidate/node/time budget yields `AMBIGUOUS` instead of
   an arbitrary pick. `AllocationBook` enforces conservation on every commit.
6. **Recompute unit = counterparty group.** Reconciliation couples all receivables and
   payments of one (normalised) counterparty, so incremental recompute re-runs whole
   groups. Each decision records:
   - direct edges `doc_version → fact → record → computation → decision`;
   - query scopes `receivables:<g>`, `bank_txns:<g>` (after reference attribution of
     payments with unknown payer), `agreements:<g>`, `config`, each with the result-set hash.
   The planner re-evaluates every recorded scope on the new snapshot, so an inserted record
   (e.g. an agreement where the decision relied on "no agreement found") invalidates the
   decision. Unknown entity kinds, unknown scope kinds, untracked decisions, a graph not
   covering existing decisions, or a missing snapshot → `fallback_full=True`.
7. **Equality proof.** `tests/unit/test_recompute_property.py` (hypothesis) applies random
   change sequences and asserts `incremental == full` for decisions, payment outcomes,
   unattributed payments *and* the dependency graph. A mutation check (scopes forced
   constant) makes this property fail, i.e. the test has detection power.
8. **Approvals.** `Approval` is bound to `result_hash` + `snapshot_hash` and never mutated.
   `result_hash` covers the decision content plus `inputs_hash` (scope result hashes and
   fact contents), so any change to a decision's evidence changes it. Review status is
   computed: APPROVED iff an approval matches the current result hash; approvals for older
   hashes → REVIEW_REQUIRED; VERIFIED = mechanical checks passed (not legal correctness).
   `approve(expected_result_hash)` runs inside `UnitOfWork.transaction(tenant)`; stale →
   `StaleResultError`. `export_report` re-checks validity at export time.
9. **CLI.** `jettae.cli:app` registers sub-apps lazily; a missing module is skipped and
   listed by `jettae modules`. Convention: modules expose `app: typer.Typer`; server modules
   may instead expose `serve` / `run` callables.

## Consequences

- A change inside one counterparty recomputes that whole group (simple and provably
  equal to full recompute; finer granularity is possible later).
- Every approval in a group becomes REVIEW_REQUIRED when any record in the group changes
  (by design: "any change touching a decision's facts or query scopes").
- `ReconConfig.time_limit_s` is a wall-clock safety cap; the node cap is the deterministic
  bound. If the time cap ever triggers, results could differ between machines — keep the
  node cap well below what the time cap allows.
- DB adapters must implement `ResultStore` (may return `None`, which forces a full
  recompute on the next change) and give `UnitOfWork.transaction` real isolation.
