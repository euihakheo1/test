"""E4 — real P2P flows (BPI Challenge 2019): descriptive statistics only.

There are no ground-truth labels in BPI 2019, so nothing here is an accuracy claim. The
report describes linking rates of the deterministic matcher, amount differences, payment
day distributions, items without clearing and processing time. Korean statutory deadlines
are not applied to this data (SPEC §3).

``evaluate`` is pure (any iterable of items, used by tests with hand-written fixtures);
``run`` writes ``data/results/bpi_eval.json`` and the E4 block of ``docs/eval_results.md``
and refuses to do so unless the input is the registered real log (manifest hash match).
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from jettae.domain.money import Money
from jettae.sources import (
    eval_results_md,
    repo_root,
    results_dir,
    sha256_file,
    upsert_md_section,
    utc_now_iso,
    write_json,
)
from jettae.sources.bpi2019 import download
from jettae.sources.bpi2019.convert import PoItem, iter_items, parse_ts
from jettae.sources.bpi2019.p2p import CURRENCY, ItemAnalysis, analyze_item, recon_config

ALL = "(all)"
MAX_LINK_EVENTS = 400


def ratio(num: int, den: int) -> str | None:
    if den == 0:
        return None
    return str((Decimal(num) / Decimal(den)).quantize(Decimal("0.0001"), ROUND_HALF_UP))


def eur(m: Money) -> str:
    return str((Decimal(m.amount) / 100).quantize(Decimal("0.01")))


def dist(values: list[int]) -> dict[str, Any]:
    """Integer distribution; percentiles by the nearest-rank method (deterministic)."""
    if not values:
        return {"n": 0}
    v = sorted(values)
    n = len(v)

    def pct(p: int) -> int:
        k = max(1, -(-p * n // 100))  # ceil(p*n/100)
        return v[k - 1]

    mean = (Decimal(sum(v)) / Decimal(n)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return {
        "n": n,
        "min": v[0],
        "p10": pct(10),
        "p25": pct(25),
        "median": pct(50),
        "p75": pct(75),
        "p90": pct(90),
        "max": v[-1],
        "mean": str(mean),
        "negative": sum(1 for x in v if x < 0),
    }


@dataclass
class Bucket:
    items: int = 0
    with_gr: int = 0
    with_ir: int = 0
    with_clear: int = 0
    ir_without_clear: int = 0
    gr_without_ir: int = 0
    linked: int = 0
    skipped_large: int = 0
    gr_ir_both: int = 0
    gr_ir_mismatch: int = 0
    gr_ir_abs_diff: Money = field(default_factory=lambda: Money.zero(CURRENCY))
    ir_clear_both: int = 0
    ir_clear_mismatch: int = 0
    ir_clear_abs_diff: Money = field(default_factory=lambda: Money.zero(CURRENCY))
    gr_status: Counter[str] = field(default_factory=Counter)
    ir_status: Counter[str] = field(default_factory=Counter)
    clear_status: Counter[str] = field(default_factory=Counter)
    days_ir: list[int] = field(default_factory=list)
    days_gr: list[int] = field(default_factory=list)
    conservation_violations: int = 0
    missing_amount_events: int = 0
    rounded_amount_events: int = 0

    def add(self, a: ItemAnalysis) -> None:
        self.items += 1
        self.with_gr += a.has_gr
        self.with_ir += a.has_ir
        self.with_clear += a.has_clear
        self.ir_without_clear += a.has_ir and not a.has_clear
        self.gr_without_ir += a.has_gr and not a.has_ir
        self.linked += a.linked
        self.skipped_large += not a.linked
        if a.has_gr and a.has_ir:
            self.gr_ir_both += 1
            if not a.gr_ir_diff.is_zero:
                self.gr_ir_mismatch += 1
                self.gr_ir_abs_diff = self.gr_ir_abs_diff + abs(a.gr_ir_diff)
        if a.has_ir and a.has_clear:
            self.ir_clear_both += 1
            if not a.ir_clear_diff.is_zero:
                self.ir_clear_mismatch += 1
                self.ir_clear_abs_diff = self.ir_clear_abs_diff + abs(a.ir_clear_diff)
        self.gr_status.update(a.gr_status)
        self.ir_status.update(a.ir_status)
        self.clear_status.update(a.clear_status)
        self.days_ir.extend(a.pay_days_from_ir)
        self.days_gr.extend(a.pay_days_from_gr)
        self.conservation_violations += (not a.gr_ir_conserved) + (not a.ir_clear_conserved)
        self.missing_amount_events += a.missing_amount
        self.rounded_amount_events += a.rounded_amounts

    def report(self) -> dict[str, Any]:
        gr_n = sum(self.gr_status.values())
        ir_n = sum(self.ir_status.values())
        return {
            "items": self.items,
            "items_with_goods_receipt": self.with_gr,
            "items_with_invoice_receipt": self.with_ir,
            "items_with_clear_invoice": self.with_clear,
            "items_invoiced_without_clearing": self.ir_without_clear,
            "items_received_without_invoice": self.gr_without_ir,
            "items_event_linking_ran": self.linked,
            "items_event_linking_skipped_large": self.skipped_large,
            "gr_ir": {
                "items_with_both": self.gr_ir_both,
                "items_amount_mismatch": self.gr_ir_mismatch,
                "mismatch_ratio": ratio(self.gr_ir_mismatch, self.gr_ir_both),
                "abs_difference_eur_total": eur(self.gr_ir_abs_diff),
                "gr_event_status": dict(sorted(self.gr_status.items())),
                "gr_event_matched_ratio": ratio(self.gr_status.get("MATCHED", 0), gr_n),
            },
            "ir_clear": {
                "items_with_both": self.ir_clear_both,
                "items_amount_mismatch": self.ir_clear_mismatch,
                "mismatch_ratio": ratio(self.ir_clear_mismatch, self.ir_clear_both),
                "abs_difference_eur_total": eur(self.ir_clear_abs_diff),
                "ir_event_status": dict(sorted(self.ir_status.items())),
                "ir_event_matched_ratio": ratio(self.ir_status.get("MATCHED", 0), ir_n),
                "clear_event_status": dict(sorted(self.clear_status.items())),
            },
            "payment_days_from_invoice_receipt": dist(self.days_ir),
            "payment_days_from_goods_receipt": dist(self.days_gr),
            "conservation_violations": self.conservation_violations,
            "events_without_amount": self.missing_amount_events,
            "amounts_rounded_to_cent": self.rounded_amount_events,
        }


DEFINITIONS = {
    "linking_scope": "one purchase-order item (XES trace); no cross-item matching",
    "gr_ir": "Record Goods Receipt (+Cancel Goods Receipt as credit) linked to Record Invoice "
    "Receipt (+Cancel Invoice Receipt as negative entry) by jettae.recon.reconcile",
    "ir_clear": "Record Invoice Receipt mapped to domain Invoice, Clear Invoice mapped to domain "
    "BankTxn, linked by jettae.recon.reconcile",
    "amount": "'Cumulative net worth (EUR)' event attribute parsed with Decimal, cents HALF_UP",
    "amount_mismatch": "item-level net totals differ (IR total - GR total, clear total - IR total)",
    "payment_days_from_invoice_receipt": "clear date - IR date, per IR<->clear allocation",
    "payment_days_from_goods_receipt": "clear date - latest GR date linked to the IR, per "
    "allocation (only when the IR was linked to a GR)",
    "business_date": "calendar date of the event timestamp in its own UTC offset",
    "percentiles": "nearest-rank",
    "ambiguous": "equal amounts that cannot be told apart stay AMBIGUOUS (no FIFO guess)",
    "statutory_rules": "not applied (BPI 2019 is not Korean retail data)",
}


def evaluate(
    items: Iterable[PoItem],
    *,
    max_link_events: int = MAX_LINK_EVENTS,
    progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Descriptive statistics over PO items (pure; no files written)."""
    t0 = time.perf_counter()
    buckets: dict[str, Bucket] = {ALL: Bucket()}
    activities: Counter[str] = Counter()
    years: Counter[str] = Counter()
    n_events = 0
    ts_min: datetime | None = None
    ts_max: datetime | None = None
    cfg = recon_config()
    for i, it in enumerate(items):
        n_events += len(it.events)
        for e in it.events:
            activities[e.activity] += 1
            dt = parse_ts(e.timestamp)
            if dt is None:
                years["(unparsable)"] += 1
                continue
            years[str(dt.year)] += 1
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            ts_min = dt if ts_min is None or dt < ts_min else ts_min
            ts_max = dt if ts_max is None or dt > ts_max else ts_max
        a = analyze_item(it, cfg, max_link_events)
        buckets[ALL].add(a)
        buckets.setdefault(a.category, Bucket()).add(a)
        if progress is not None and (i + 1) % 10000 == 0:
            progress(i + 1)
    elapsed = time.perf_counter() - t0
    n_items = buckets[ALL].items
    return {
        "eval": "E4",
        "kind": "descriptive statistics (no ground truth; not an accuracy claim)",
        "log": {
            "items": n_items,
            "events": n_events,
            "activity_counts": dict(activities.most_common()),
            "event_years": dict(sorted(years.items())),
            "timestamp_min": ts_min.isoformat() if ts_min else None,
            "timestamp_max": ts_max.isoformat() if ts_max else None,
        },
        "overall": buckets[ALL].report(),
        "by_item_category": {k: b.report() for k, b in sorted(buckets.items()) if k != ALL},
        "processing": {
            "seconds": str(Decimal(elapsed).quantize(Decimal("0.1"))),
            "items_per_second": str(
                (Decimal(n_items) / Decimal(elapsed)).quantize(Decimal("1"))
                if elapsed > 0
                else "n/a"
            ),
            "max_link_events_per_item": max_link_events,
        },
        "recon_config": {
            "days_before": cfg.days_before,
            "days_after": cfg.days_after,
            "max_candidates": cfg.max_candidates,
            "max_subset_size": cfg.max_subset_size,
            "max_nodes": cfg.max_nodes,
        },
        "definitions": DEFINITIONS,
    }


class NotRealData(RuntimeError):
    pass


def _registered_input(path: Path | None) -> tuple[Path, dict[str, Any]]:
    m = download.load_manifest()
    f = m.get("file") or {}
    reg = download.local_log_path()
    if reg is None:
        raise NotRealData(
            "E4 not run: the real BPI Challenge 2019 log is not available (manifest status: "
            f"{m.get('status')}). Run `{download.FETCH_CMD}` (or `--file <path>`) first."
        )
    if path is not None and path.resolve() != reg.resolve():
        # a converted items file derived from the registered log is accepted
        conv = (m.get("converted") or {}).get("path")
        conv_ok = conv and (repo_root() / conv).resolve() == path.resolve()
        if not conv_ok:
            raise NotRealData(f"{path} is not the registered BPI 2019 log or its conversion")
        return path, m
    return reg, f


def run(
    path: Path | None = None,
    *,
    limit: int | None = None,
    out: Path | None = None,
    md: Path | None = None,
    progress: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Run E4 on the registered real log (or its conversion) and write results."""
    src, _ = _registered_input(path)
    m = download.load_manifest()
    res = evaluate(iter_items(src, limit=limit), progress=progress)
    res["run"] = {
        "at": utc_now_iso(),
        "input": str(src),
        "input_sha256": sha256_file(src),
        "source_manifest": "data/manifests/bpi2019.json",
        "source_sha256": (m.get("file") or {}).get("sha256"),
        "doi": download.DOI,
        "license": download.LICENSE,
        "limit": limit,
        "partial": limit is not None,
        "command": "uv run jettae sources bpi2019 stats"
        + (f" --limit {limit}" if limit is not None else ""),
        "provenance_warning": download.provenance_warning(m),
    }
    out = out or results_dir() / "bpi_eval.json"
    write_json(out, res)
    upsert_md_section(md or eval_results_md(), "E4", render_md(res))
    return res


def render_md(res: dict[str, Any]) -> str:
    o = res["overall"]
    run = res["run"]
    scope = f"PARTIAL (first {run['limit']} items)" if run["partial"] else "full log"
    lines = [
        "## E4 — BPI Challenge 2019 (real P2P flows, descriptive)",
        "",
        f"Run {res['run']['at']} · input sha256 `{res['run']['input_sha256'][:16]}…` · "
        f"{scope}"
        f" · `{res['run']['command']}`",
        "",
        *(
            [
                f"**Input provenance: {run['provenance_warning']}** — not confirmed to be the "
                "published BPI_Challenge_2019.xes.",
                "",
            ]
            if run.get("provenance_warning")
            else []
        ),
        "No ground truth exists for this log; these are descriptive statistics, not accuracy.",
        "Korean statutory deadlines are not applied. Full numbers: `data/results/bpi_eval.json`.",
        "",
        "| metric | value |",
        "|---|---|",
        f"| PO items / events | {res['log']['items']} / {res['log']['events']} |",
        f"| items with GR / IR / clear | {o['items_with_goods_receipt']} / "
        f"{o['items_with_invoice_receipt']} / {o['items_with_clear_invoice']} |",
        f"| items invoiced without clearing | {o['items_invoiced_without_clearing']} |",
        f"| GR-IR item amount mismatch | {o['gr_ir']['items_amount_mismatch']} of "
        f"{o['gr_ir']['items_with_both']} ({o['gr_ir']['mismatch_ratio']}) |",
        f"| IR-clear item amount mismatch | {o['ir_clear']['items_amount_mismatch']} of "
        f"{o['ir_clear']['items_with_both']} ({o['ir_clear']['mismatch_ratio']}) |",
        f"| GR events linked (MATCHED ratio) | {o['gr_ir']['gr_event_matched_ratio']} |",
        f"| IR events linked to clearing (MATCHED ratio) | "
        f"{o['ir_clear']['ir_event_matched_ratio']} |",
        f"| payment days from IR (median / p90) | "
        f"{o['payment_days_from_invoice_receipt'].get('median')} / "
        f"{o['payment_days_from_invoice_receipt'].get('p90')} |",
        f"| payment days from GR (median / p90) | "
        f"{o['payment_days_from_goods_receipt'].get('median')} / "
        f"{o['payment_days_from_goods_receipt'].get('p90')} |",
        f"| conservation violations | {o['conservation_violations']} |",
        f"| processing | {res['processing']['seconds']} s "
        f"({res['processing']['items_per_second']} items/s) |",
    ]
    return "\n".join(lines)


def render_pending_md(reason: str) -> str:
    return "\n".join(
        [
            "## E4 — BPI Challenge 2019 (real P2P flows, descriptive)",
            "",
            f"**Not run** ({utc_now_iso()}): {reason}",
            "",
            "No numbers are reported until the real log is processed. To run:",
            "",
            "```bash",
            f"{download.FETCH_CMD}            # or: {download.FETCH_CMD} --file <path>",
            "uv run jettae sources bpi2019 convert",
            "uv run jettae sources bpi2019 stats",
            "```",
        ]
    )


def record_pending(reason: str, md: Path | None = None) -> None:
    upsert_md_section(md or eval_results_md(), "E4", render_pending_md(reason))


def run_cli(file: Path | None = None, limit: int | None = None) -> None:
    """E4: BPI 2019 기술 통계(등록된 실제 로그 또는 그 변환 파일에서만)."""
    src = file
    if src is None:
        conv = (download.load_manifest().get("converted") or {}).get("path")
        if conv and (repo_root() / conv).exists():
            src = repo_root() / conv
    try:
        res = run(src, limit=limit, progress=lambda n: print(f"  {n} items"))
    except NotRealData as e:
        print(e)
        record_pending(str(e))
        raise SystemExit(3) from None
    print(render_md(res))
