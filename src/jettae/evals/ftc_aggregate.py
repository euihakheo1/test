"""FTC evaluation, aggregation: E2 / E2-range / E2-cond / E3 summaries and E1 case checks."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any

from jettae.evals.ftc_inputs import VARIANTS, SeedRow, TableMeta, parse_int_cell, variant_label
from jettae.evals.ftc_rows import RowResult, VariantCheck


def _rate(n: int, d: int) -> dict[str, Any]:
    return {"n": n, "of": d, "rate": (round(n / d, 4) if d else None)}


CHECKS = ("due_ok", "delay_ok", "interest_ok")


def _variant_metrics(vals: list[VariantCheck], n_rows: int) -> dict[str, Any]:
    """Per-variant rates. Single checks: over rows where that check is evaluable.
    ``all_three_ok``: over ALL rows (non-evaluable = not matched); the same count over the
    fully evaluable rows is ``all_three_ok_of_fully_evaluable``."""
    per: dict[str, Any] = {}
    for what in CHECKS:
        ev = [v for v in vals if getattr(v, what) is not None]
        per[what] = _rate(sum(1 for v in ev if getattr(v, what)), len(ev))
    full = [v for v in vals if v.fully_evaluated]
    n_all3 = sum(1 for v in vals if v.all_ok)
    avail = [v for v in vals if v.evaluated]
    per["all_three_ok"] = _rate(n_all3, n_rows)
    per["all_three_ok_of_fully_evaluable"] = _rate(n_all3, len(full))
    per["all_available_ok"] = _rate(sum(1 for v in avail if v.all_available_ok), len(avail))
    per["fully_evaluable"] = _rate(len(full), n_rows)
    per["engine_full_computation"] = _rate(sum(1 for v in vals if v.fully_computed), n_rows)
    return per


def summarize_e2(results: list[RowResult], *, certain_only: bool = False) -> dict[str, Any]:
    """E2 summary for rows with a base date (metric definitions: ftc_eval module docstring)."""
    rows = [r for r in results if r.mode == "e2" and (not certain_only or not r.uncertain)]
    n = len(rows)
    out: dict[str, Any] = {
        "rows": n,
        "abstained": _rate(sum(1 for r in rows if r.abstained), n),
        "engine_full_computation": _rate(sum(1 for r in rows if r.fully_computed), n),
        "evaluable_checks_per_row": {
            str(k): sum(1 for r in rows if r.evaluable_checks == k) for k in range(4)
        },
        "evaluable_rows_per_check": {
            what: sum(1 for r in rows if any(getattr(v, what) is not None for v in r.variants))
            for what in CHECKS
        },
        "per_variant": {},
    }
    for rollover, rounding in VARIANTS:
        lab = variant_label(rollover, rounding)
        vals = [v for r in rows for v in r.variants if v.label == lab]
        out["per_variant"][lab] = _variant_metrics(vals, n)
    # Post-hoc: a row counts if ANY variant matches -- the variant is picked after seeing the
    # table, so this is an upper bound ("제시한 계산 중 정답을 포함한 비율"), not an accuracy.
    post: dict[str, Any] = {}
    for what in CHECKS:
        ev = [r for r in rows if any(getattr(v, what) is not None for v in r.variants)]
        post[what] = _rate(sum(1 for r in ev if any(getattr(v, what) for v in r.variants)), len(ev))
    post["all_three_ok"] = _rate(sum(1 for r in rows if any(v.all_ok for v in r.variants)), n)
    avail = [r for r in rows if r.evaluable_checks]
    post["all_available_ok"] = _rate(
        sum(1 for r in avail if any(v.all_available_ok for v in r.variants)), len(avail)
    )
    out["post_hoc_any_variant"] = post
    out["interest_not_computed_by_engine"] = sum(1 for r in rows if r.interest_engine_missing)
    arith_rows = [r for r in rows if r.arith]
    if arith_rows:
        arith: dict[str, Any] = {}
        for rollover, rounding in VARIANTS:
            lab = variant_label(rollover, rounding)
            oks = [
                r.arith[lab][rounding.value]["ok"]
                for r in arith_rows
                if r.arith.get(lab, {}).get("computed")
            ]
            arith[lab] = _rate(sum(1 for o in oks if o), len(oks))
        out["interest_with_stated_rate_when_engine_missing"] = arith
    by_table: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        ok = tuple(r.matched("all_available_ok"))
        by_table[f"{r.decision_id}/{r.table_label}"]["|".join(ok) if ok else "(none)"] += 1
    out["all_available_matched_variant_sets_by_table"] = {k: dict(v) for k, v in by_table.items()}
    return out


def summarize_range(results: list[RowResult]) -> dict[str, Any]:
    all_rows = [r for r in results if r.mode == "e2_range"]
    rows = [r for r in all_rows if not r.rule_missing]
    out: dict[str, Any] = {
        "rows": len(all_rows),
        "not_computed_no_rule_version": len(all_rows) - len(rows),
        "computed": len(rows),
    }
    for lab in ("rollover_off", "rollover_on"):
        vals = [v for r in rows for v in r.variants if v.label == lab]
        out[lab] = _rate(sum(1 for v in vals if v.due_ok), len(vals))
    # post hoc: either rollover setting matched (picked after seeing the table)
    out["post_hoc_any_variant"] = _rate(
        sum(1 for r in rows if any(v.due_ok for v in r.variants)), len(rows)
    )
    return out


def summarize_cond(results: list[RowResult]) -> dict[str, Any]:
    rows = [r for r in results if r.mode == "e2_cond"]
    out: dict[str, Any] = {"rows": len(rows)}
    for lab in ("table_due/floor", "table_due/half_up"):
        vals = [v for r in rows for v in r.variants if v.label == lab]
        out[lab] = {
            "delay_ok": _rate(
                sum(1 for v in vals if v.delay_ok), sum(1 for v in vals if v.delay_ok is not None)
            ),
            "interest_ok": _rate(
                sum(1 for v in vals if v.interest_ok),
                sum(1 for v in vals if v.interest_ok is not None),
            ),
        }
    return out


def summarize_e3(results: list[RowResult], rows: list[SeedRow]) -> dict[str, Any]:
    by_key = {r.key: r for r in rows}
    no_base = [r for r in results if r.abstained is not None and not by_key[r.key]["base_date"]]
    with_base = [r for r in results if r.abstained is not None and by_key[r.key]["base_date"]]
    docs = Counter(d for r in no_base for d in r.required_documents)
    base_keys = {"goods_received_date", "sales_close_date", "object_received_date"}
    no_rule = [r for r in with_base if r.abstained and r.rule_missing]
    return {
        "rows_without_base_date": len(no_base),
        "abstained_correctly": _rate(
            sum(1 for r in no_base if r.abstained and base_keys & set(r.abstain_missing)),
            len(no_base),
        ),
        "with_required_documents": _rate(
            sum(1 for r in no_base if r.abstained and r.required_documents), len(no_base)
        ),
        "rows_with_base_date": len(with_base),
        "abstained_no_rule_version": _rate(len(no_rule), len(with_base)),
        "no_rule_version_rows": [r.key for r in no_rule],
        "false_abstentions": _rate(
            sum(1 for r in with_base if r.abstained and not r.rule_missing), len(with_base)
        ),
        "required_documents_listed": dict(docs),
    }


# ------------------------------------------------------------------ E1
def _col_total(meta: TableMeta, kind: str) -> int | None:
    if kind == "total_unpaid_interest":
        v = parse_int_cell(meta["printed_total_unpaid_interest"])
        if v is None and not meta["unpaid_interest_col"]:
            # the table has a single interest column; the text calls it unpaid interest
            v = parse_int_cell(meta["printed_total_interest"])
        return v
    if kind == "total_interest":
        return parse_int_cell(meta["printed_total_interest"])
    if kind == "total_delayed_principal":
        return parse_int_cell(meta["printed_total_principal"])
    return None


def evaluate_e1(
    tables: dict[tuple[str, str], TableMeta],
    rows: list[SeedRow],
    results: list[RowResult],
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    by_label: dict[tuple[str, str], TableMeta] = {
        (m["decision_id"], m["table_label"]): m for m in tables.values()
    }
    res_by_table: dict[tuple[str, str], list[RowResult]] = defaultdict(list)
    for r in results:
        res_by_table[(r.decision_id, r.table_label)].append(r)
    rows_by_table: dict[tuple[str, str], list[SeedRow]] = defaultdict(list)
    for row in rows:
        rows_by_table[(row["decision_id"], row["table_label"])].append(row)

    checks: list[dict[str, Any]] = []
    decisions = {m["decision_id"] for m in tables.values()}
    amount_kinds = ("total_unpaid_interest", "total_interest", "total_delayed_principal")
    seen: dict[tuple[str, str, str, tuple[str, ...]], dict[str, Any]] = {}
    for f in facts:
        if f["decision_id"] not in decisions or f["masked"]:
            continue
        refs = [t for t in f.get("table_refs", []) if (f["decision_id"], t) in by_label]
        if not refs:
            continue
        dkey = (f["decision_id"], f["kind"], json.dumps(f["value"], sort_keys=True), tuple(refs))
        if dkey in seen:  # the same claim repeated later in the text
            seen[dkey]["mentions"] += 1
            continue
        metas = [by_label[(f["decision_id"], t)] for t in refs]
        base = {
            "decision_id": f["decision_id"],
            "fact_kind": f["kind"],
            "text_value": f["value"],
            "text_raw": f["raw"],
            "span": {k: f[k] for k in ("section", "char_start", "char_end")},
            "tables": refs,
            "confidence": f["confidence"],
            "mentions": 1,
        }
        seen[dkey] = base
        if f["kind"] in amount_kinds:
            totals = [_col_total(m, f["kind"]) for m in metas]
            mult = {m.multiplier for m in metas}
            chk = base
            if all(t is not None for t in totals) and len(mult) == 1:
                printed = sum(t for t in totals if t is not None) * mult.pop()
                chk["printed_table_total_krw"] = printed
                chk["text_minus_printed_krw"] = int(f["value"]) - printed
                diff = abs(chk["text_minus_printed_krw"])
                if diff:
                    note = next(
                        (
                            n
                            for n in facts
                            if n["decision_id"] == f["decision_id"]
                            and n["kind"] == "rounding_note"
                            and re.search(
                                rf"(?<![\d,]){diff:,}\s*원|(?<![\d,]){diff}\s*원", n["raw"]
                            )
                        ),
                        None,
                    )
                    if note is not None:
                        chk["difference_stated_in_text"] = {
                            k: note[k] for k in ("section", "char_start", "char_end", "value")
                        }
            else:
                chk["printed_table_total_krw"] = None
            chk.update(_engine_total(f["kind"], metas, res_by_table, rows_by_table))
            checks.append(chk)
        elif f["kind"] == "delay_days_range":
            chk = base
            chk.update(_engine_delay_range(metas, res_by_table, rows_by_table))
            if chk.get("engine_computable"):
                v = f["value"]
                chk["min_ok_by_variant"] = {
                    k: x == v["min"] for k, x in chk["engine_min_by_variant"].items()
                }
                chk["max_ok_by_variant"] = {
                    k: x == v["max"] for k, x in chk["engine_max_by_variant"].items()
                }
            checks.append(chk)
        elif f["kind"] in ("supplier_count", "transaction_count"):
            unit = "supplier" if f["kind"] == "supplier_count" else "transaction"
            if not all(m["serial_unit"] == unit for m in metas):
                continue  # 연번 of these tables does not count this unit
            chk = base
            serials = [
                row["serial_no"]
                for m in metas
                for row in rows_by_table[(m["decision_id"], m["table_label"])]
                if row["serial_no"]
            ]
            # no engine involved: counts come from the transcribed 연번 column
            chk["engine_computable"] = None
            if all(m.complete for m in metas):
                chk["transcribed_distinct_serials"] = len(set(serials))
                chk["equal"] = len(set(serials)) == f["value"]
            elif serials and all(s.isdigit() for s in serials):
                chk["last_serial_no"] = max(int(s) for s in serials)
                chk["equal"] = chk["last_serial_no"] == f["value"]
                chk["note"] = "rows elided (⋮): compared with the last 연번 printed"
            checks.append(chk)

    text_vs_printed = [c for c in checks if c.get("printed_table_total_krw") is not None]
    computable = [c for c in checks if c.get("engine_computable")]
    return {
        "checks": checks,
        "summary": {
            "facts_linked_to_transcribed_tables": len(checks),
            "text_vs_printed_total": {
                "compared": len(text_vs_printed),
                "equal": sum(1 for c in text_vs_printed if c["text_minus_printed_krw"] == 0),
                "differences_krw": [c["text_minus_printed_krw"] for c in text_vs_printed],
            },
            "engine_computable": len(computable),
            # registry = interest from the engine's rule registry; stated_rate = engine
            # delay days x the rate stated in the decision itself; mixed = both kinds of
            # rows; no_rate = checks without interest (e.g. delay-day ranges)
            "engine_computable_by_source": {
                src: sum(1 for c in computable if c.get("interest_source", "no_rate") == src)
                for src in ("registry", "stated_rate", "mixed", "no_rate")
            },
            "not_computable_rows_elided": sum(
                1 for c in checks if "elided" in str(c.get("reason", ""))
            ),
            "count_checks": {
                "compared": sum(1 for c in checks if "equal" in c),
                "equal": sum(1 for c in checks if c.get("equal") is True),
            },
        },
        "transcription_qa": _transcription_qa(tables, rows_by_table),
    }


def _engine_total(
    kind: str,
    metas: list[TableMeta],
    res_by_table: dict[tuple[str, str], list[RowResult]],
    rows_by_table: dict[tuple[str, str], list[SeedRow]],
) -> dict[str, Any]:
    if not all(m.complete for m in metas):
        return {"engine_computable": False, "reason": "rows elided (⋮) in the table excerpt"}
    if kind != "total_unpaid_interest" and kind != "total_interest":
        return {"engine_computable": False, "reason": "engine sums only interest"}
    sums: dict[str, int | None] = {}
    # Metric definition: a row's interest is "registry" when the engine computed it with an
    # interest-notice version from the rule registry, "stated_rate" when the registry has no
    # version for the delay period and the row was computed from the engine's delay days
    # with the rate stated in the SAME decision text whose total is being checked (so such
    # a match is partly circular and must not be presented as an engine result).
    registry_rows: set[int] = set()
    stated_rows: set[int] = set()
    for rollover, rounding in VARIANTS:
        lab = variant_label(rollover, rounding)
        total, ok = 0, True
        for m in metas:
            for r in res_by_table[(m["decision_id"], m["table_label"])]:
                if r.mode != "e2":
                    ok = False
                    continue
                v = next((x for x in r.variants if x.label == lab), None)
                if v is not None and v.interest is not None:
                    total += v.interest
                    registry_rows.add(id(r))
                    continue
                arith = r.arith.get(lab) if r.arith else None
                if isinstance(arith, dict) and arith.get("computed"):
                    total += int(arith[rounding.value]["interest_krw"])
                    stated_rows.add(id(r))
                    continue
                ok = False
        sums[lab] = total if ok else None
    if stated_rows and registry_rows:
        source = "mixed"
    elif stated_rows:
        source = "stated_rate"
    else:
        source = "registry"
    return {
        "engine_computable": any(v is not None for v in sums.values()),
        "engine_sum_by_variant_krw": sums,
        "interest_source": source,
        "rows_registry_rate": len(registry_rows - stated_rows),
        "rows_stated_rate": len(stated_rows),
        "engine_sum_note": "rows whose interest the engine registry does not cover use the "
        "rate stated in the decision text (see E2 'interest_with_stated_rate')",
    }


def _engine_delay_range(
    metas: list[TableMeta],
    res_by_table: dict[tuple[str, str], list[RowResult]],
    rows_by_table: dict[tuple[str, str], list[SeedRow]],
) -> dict[str, Any]:
    if not all(m.complete for m in metas):
        return {"engine_computable": False, "reason": "rows elided (⋮) in the table excerpt"}
    mins: dict[str, int] = {}
    maxs: dict[str, int] = {}
    for rollover, rounding in VARIANTS:
        lab = variant_label(rollover, rounding)
        ds = [
            v.delay_days
            for m in metas
            for r in res_by_table[(m["decision_id"], m["table_label"])]
            for v in r.variants
            if v.label == lab and v.delay_days is not None
        ]
        if ds:
            mins[lab], maxs[lab] = min(ds), max(ds)
    return {
        "engine_computable": bool(mins),
        "engine_min_by_variant": mins,
        "engine_max_by_variant": maxs,
    }


def _transcription_qa(
    tables: dict[tuple[str, str], TableMeta], rows_by_table: dict[tuple[str, str], list[SeedRow]]
) -> list[dict[str, Any]]:
    """Column sums of transcribed rows vs printed 합계 (complete tables only; no engine)."""
    out = []
    for m in tables.values():
        if not m.complete:
            continue
        rows = [r for r in rows_by_table[(m["decision_id"], m["table_label"])] if not r.is_range]
        item: dict[str, Any] = {"decision_id": m["decision_id"], "table_label": m["table_label"]}
        for col, tot in (
            ("principal_krw", "printed_total_principal"),
            ("interest_in_table", "printed_total_interest"),
            ("unpaid_interest_in_table", "printed_total_unpaid_interest"),
        ):
            printed = parse_int_cell(m[tot])
            if printed is None:
                continue
            vals = [parse_int_cell(r[col]) for r in rows]
            s = sum(v for v in vals if v is not None)
            item[col] = {"sum": s, "printed": printed, "diff": s - printed}
        out.append(item)
    return out
