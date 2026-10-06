"""Evaluations on real public data only (SPEC §6). No synthetic datasets.

- :mod:`.ftc_eval` — E1 (case-level), E2 (row-level), E3 (abstention) on 공정위 의결서
  (split into ``ftc_inputs`` / ``ftc_rows`` / ``ftc_aggregate`` / ``ftc_report``).
- :mod:`.bpi_eval` (E4), :mod:`.contract_eval` (E5), :mod:`.recompute_eval` (E6).

:mod:`.cli` mounts each evaluation module lazily: a module exposing ``app: typer.Typer`` or a
``main`` / ``run_cli`` callable becomes ``jettae eval <name>``; a missing module is skipped.
Each evaluation owns one marked block in ``docs/eval_results.md`` and replaces only that block.
"""
