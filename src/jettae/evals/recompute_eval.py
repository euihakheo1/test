"""E6 — full vs incremental recompute on real records with real change sequences.

Scenarios (each used only when its real input exists; nothing is invented):

- ``ftc_rows``: rows transcribed from FTC decisions (``data/seeds/ftc_rows.csv``). Base
  snapshot = receivables (one per row); changes = the row's payments added in payment-date
  order, then correction rows present in the file (``supersedes`` column) as updates.
- ``bpi2019``: a deterministic prefix of the converted BPI 2019 log. Base snapshot = invoice
  receipts (domain ``Invoice``); changes = ``Clear Invoice`` events (domain ``BankTxn``) and
  ``Cancel Invoice Receipt`` events (credit ``Invoice``) in event-time order.

After every change, ``incremental_recompute`` (chained from the previous incremental result)
is compared with ``full_recompute`` of the same snapshot: decisions, groups, unattributed
payments and the dependency graph must be identical. Recompute volume is reported as
recomputed counterparty groups / decisions vs. the full count.
"""

from __future__ import annotations

import csv
import dataclasses
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from jettae.domain.models import BankTxn, Change, Invoice
from jettae.domain.money import Money
from jettae.domain.status import ChangeKind, TradeType
from jettae.evidence import full_recompute, incremental_recompute
from jettae.evidence.snapshot import AnalysisConfig, Snapshot
from jettae.recon import ReconConfig
from jettae.sources import (
    data_dir,
    eval_results_md,
    repo_root,
    results_dir,
    sha256_file,
    upsert_md_section,
    utc_now_iso,
    write_json,
)

CMD = "uv run jettae eval recompute"


@dataclass
class Scenario:
    name: str
    base: Snapshot
    changes: list[Change]
    provenance: dict[str, Any] = field(default_factory=dict)


def _ms(seconds: float) -> str:
    return str(Decimal(seconds * 1000).quantize(Decimal("0.1")))


def run_scenario(sc: Scenario) -> dict[str, Any]:
    snap = sc.base
    t0 = time.perf_counter()
    prev = full_recompute(snap)
    base_ms = time.perf_counter() - t0
    steps: list[dict[str, Any]] = []
    t_inc = t_full = 0.0
    for i, ch in enumerate(sc.changes):
        snap = snap.apply(ch)
        a = time.perf_counter()
        inc, plan, groups = incremental_recompute(prev, snap, [ch])
        b = time.perf_counter()
        full = full_recompute(snap)
        c = time.perf_counter()
        t_inc += b - a
        t_full += c - b
        equal = inc.comparable() == full.comparable()
        graph_equal = inc.graph == full.graph
        n_dec = sum(len(full.groups[g].decision_ids) for g in groups if g in full.groups)
        steps.append(
            {
                "step": i + 1,
                "change": f"{ch.kind.value} {ch.entity} {ch.entity_id}",
                "fallback_full": plan.fallback_full,
                "recomputed_groups": len(groups),
                "total_groups": len(full.groups),
                "recomputed_decisions": n_dec,
                "total_decisions": len(full.decisions),
                "equal": equal,
                "graph_equal": graph_equal,
            }
        )
        prev = inc
    n = len(steps)
    tot_g = sum(s["total_groups"] for s in steps)
    tot_d = sum(s["total_decisions"] for s in steps)
    rec_g = sum(s["recomputed_groups"] for s in steps)
    rec_d = sum(s["recomputed_decisions"] for s in steps)
    mismatches = [s for s in steps if not (s["equal"] and s["graph_equal"])]
    return {
        "scenario": sc.name,
        "provenance": sc.provenance,
        "base": {
            "receivables": len(sc.base.receivables),
            "bank_txns": len(sc.base.bank_txns),
            "groups": len(prev.groups) if n == 0 else steps[0]["total_groups"],
            "full_recompute_ms": _ms(base_ms),
        },
        "changes": n,
        "mismatching_steps": len(mismatches),
        "all_equal": not mismatches,
        "fallback_full_steps": sum(1 for s in steps if s["fallback_full"]),
        "steps_recomputing_nothing": sum(1 for s in steps if s["recomputed_groups"] == 0),
        "recomputed_groups_total": rec_g,
        "full_groups_total": tot_g,
        "recomputed_decisions_total": rec_d,
        "full_decisions_total": tot_d,
        "recomputed_decision_share": (
            str((Decimal(rec_d) / Decimal(tot_d)).quantize(Decimal("0.0001"))) if tot_d else None
        ),
        "incremental_ms_total": _ms(t_inc),
        "full_ms_total": _ms(t_full),
        "first_mismatches": mismatches[:5],
        "steps": steps,
    }


# ----------------------------------------------------------------------- FTC rows
ALIASES: dict[str, tuple[str, ...]] = {
    "case": ("case_id", "decision_id", "flseq", "fl_seq", "사건번호", "의결번호", "case"),
    "row": ("row_id", "row_idx", "row", "row_no", "행", "행번호", "번호"),
    "table": ("table", "table_label", "table_no", "표", "표번호"),
    "counterparty": (
        "supplier",
        "counterparty",
        "vendor",
        "납품업자",
        "거래처",
        "납품업체",
        "업체명",
        "serial_no",  # 연번 of the supplier in a decision table (names are masked)
    ),
    "received": ("goods_received_date", "received_date", "상품수령일", "수령일", "입고일"),
    "sales_close": ("sales_close_date", "판매마감일", "월판매마감일"),
    "base": ("base_date", "기준일", "기산일"),
    "base_kind": ("base_date_kind", "기준일종류"),
    "paid": ("paid_date", "payment_date", "지급일", "대금지급일"),
    "amount": (
        "amount",
        "principal_krw",
        "principal",
        "금액",
        "지급금액",
        "상품대금",
        "대금",
        "미지급금액",
    ),
    "trade_type": ("trade_type", "deal_type", "거래형태", "거래유형"),
    "supersedes": ("supersedes", "corrects", "정정대상", "정정"),
    "verified": ("verified", "검증"),
}
_DATE_RE = re.compile(r"^\s*(\d{4})[-./]?\s*(\d{1,2})[-./]?\s*(\d{1,2})\.?\s*$")
_TRADE = {
    "direct": TradeType.DIRECT,
    "직매입": TradeType.DIRECT,
    "consignment": TradeType.CONSIGNMENT,
    "특약매입": TradeType.CONSIGNMENT,
    "위수탁": TradeType.CONSIGNMENT,
    "subcontract": TradeType.SUBCONTRACT,
    "하도급": TradeType.SUBCONTRACT,
}


def _parse_date(v: str | None) -> date | None:
    if not v:
        return None
    m = _DATE_RE.match(v)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _parse_amount(v: str | None) -> int | None:
    if v is None:
        return None
    s = re.sub(r"[,\s원₩]", "", v)
    if not re.fullmatch(r"-?\d+", s):
        return None
    return int(s)


def map_columns(header: Iterable[str]) -> dict[str, str]:
    norm = {re.sub(r"\s+", "", h).lower(): h for h in header}
    out: dict[str, str] = {}
    for key, names in ALIASES.items():
        for n in names:
            if n.lower() in norm:
                out[key] = norm[n.lower()]
                break
    return out


class ScenarioUnavailable(RuntimeError):
    pass


def _base_field(kind: str, trade: TradeType | None) -> str:
    """Which statutory base-date field a generic ``base_date`` column holds."""
    k = re.sub(r"\s+", "", kind)
    if "마감" in k or "sales_close" in k:
        return "sales_close_date"
    if any(w in k for w in ("수령", "입고", "하차", "received")):
        return "goods_received_date"
    return "sales_close_date" if trade is TradeType.CONSIGNMENT else "goods_received_date"


def ftc_scenario(path: Path, tenant: str = "ftc") -> Scenario:
    if not path.exists():
        raise ScenarioUnavailable(f"{path.name} not present")
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = map_columns(reader.fieldnames or [])
        rows = list(reader)
    if not {"amount", "paid"} <= cols.keys():
        raise ScenarioUnavailable(
            f"unrecognised columns {reader.fieldnames}: need an amount and a paid-date column"
        )

    def get(r: dict[str, str], key: str) -> str:
        return (r.get(cols[key]) or "").strip() if key in cols else ""

    invoices: dict[str, Invoice] = {}
    payments: list[BankTxn] = []
    corrections: list[tuple[date, Invoice]] = []
    skipped = 0
    unverified = 0
    for i, r in enumerate(rows, start=1):
        amt = _parse_amount(get(r, "amount") or None)
        if amt is None:
            skipped += 1
            continue
        case = get(r, "case") or "case"
        table = re.sub(r"\s+", "", get(r, "table"))
        rowid = get(r, "row") or str(i)
        rid = f"ftc:{case}:{table}:{rowid}" if table else f"ftc:{case}:{rowid}"
        cp = get(r, "counterparty") or "unknown"
        tt_raw = get(r, "trade_type")
        trade = _TRADE.get(tt_raw.lower()) or _TRADE.get(tt_raw)
        received = _parse_date(get(r, "received")) if "received" in cols else None
        close = _parse_date(get(r, "sales_close")) if "sales_close" in cols else None
        generic = _parse_date(get(r, "base")) if "base" in cols else None
        if generic is not None:
            if _base_field(get(r, "base_kind"), trade) == "sales_close_date":
                close = close or generic
            else:
                received = received or generic
        if get(r, "verified").lower() in ("no", "false", "0", "n"):
            unverified += 1
        inv = Invoice(
            id=rid,
            tenant_id=tenant,
            # '|' survives counterparty normalisation, so case/table/supplier keys stay distinct
            counterparty=f"{case}|{table}|{cp}",
            amount=Money(amt),
            trade_type=trade,
            goods_received_date=received,
            sales_close_date=close,
            reference=rid,
        )
        sup = get(r, "supersedes")
        paid = _parse_date(get(r, "paid"))
        if sup:
            target = f"ftc:{case}:{table}:{sup}" if table else f"ftc:{case}:{sup}"
            corrections.append((paid or date.max, dataclasses.replace(inv, id=target)))
            continue
        if rid in invoices:
            skipped += 1
            continue
        invoices[rid] = inv
        if paid is not None and amt > 0:
            payments.append(
                BankTxn(
                    id=f"pay:{rid}",
                    tenant_id=tenant,
                    booked_date=paid,
                    amount=Money(amt),
                    counterparty=inv.counterparty,
                    reference=rid,
                )
            )
    if not invoices:
        raise ScenarioUnavailable("no usable rows")
    payments.sort(key=lambda t: (t.booked_date, t.id))
    changes = [Change(ChangeKind.ADD, "bank_txn", t.id, t) for t in payments]
    n_corr = 0
    for _d, inv in sorted(corrections, key=lambda x: (x[0], x[1].id)):
        if inv.id in invoices:
            changes.append(Change(ChangeKind.UPDATE, "invoice", inv.id, inv))
            n_corr += 1
    as_of = max((t.booked_date for t in payments), default=None)
    base = Snapshot(tenant, invoices=tuple(invoices.values()), config=AnalysisConfig(as_of=as_of))
    return Scenario(
        "ftc_rows",
        base,
        changes,
        {
            "file": _rel(path),
            "sha256": sha256_file(path),
            "rows": len(rows),
            "rows_skipped": skipped,
            "rows_marked_unverified": unverified,
            "receivables": len(invoices),
            "payment_changes": len(payments),
            "correction_changes": n_corr,
            "receivables_with_base_date": sum(
                1 for v in invoices.values() if v.goods_received_date or v.sales_close_date
            ),
            "columns_used": cols,
            "change_order": "payments by paid date, then correction rows (if any)",
        },
    )


# ----------------------------------------------------------------------- BPI 2019
def bpi_scenario(path: Path, max_items: int = 300, max_changes: int = 300) -> Scenario:
    from jettae.sources.bpi2019.convert import iter_items
    from jettae.sources.bpi2019.p2p import CURRENCY, analyze_item

    if not path.exists():
        raise ScenarioUnavailable(f"{path} not present")
    tenant = "bpi2019"
    invoices: list[Invoice] = []
    changes: list[tuple[date, int, Change]] = []
    n_items = 0
    for it in iter_items(path):
        if n_items >= max_items:
            break
        a = analyze_item(it)
        if not a.invoices:
            continue
        n_items += 1
        for inv in a.invoices:
            gid = f"{it.id}:{inv.id}"
            rec = dataclasses.replace(inv, id=gid, tenant_id=tenant, counterparty=it.vendor)
            if inv.id.startswith("irc"):  # Cancel Invoice Receipt -> later correction
                changes.append(
                    (inv.issue_date or date.max, 1, Change(ChangeKind.ADD, "invoice", gid, rec))
                )
            else:
                invoices.append(rec)
        for t in a.payments:
            gid = f"{it.id}:{t.id}"
            rec_t = dataclasses.replace(t, id=gid, tenant_id=tenant, counterparty=it.vendor)
            changes.append((t.booked_date, 0, Change(ChangeKind.ADD, "bank_txn", gid, rec_t)))
    if not invoices:
        raise ScenarioUnavailable("no PO items with invoice receipts in the input")
    changes.sort(key=lambda x: (x[0], x[1], x[2].entity_id))
    picked = [c for _d, _k, c in changes[:max_changes]]
    dates = [d for d, _k, _c in changes[:max_changes] if d != date.max]
    cfg = AnalysisConfig(
        as_of=max(dates) if dates else None,
        recon=ReconConfig(fee_tolerance=Money.zero(CURRENCY), max_nodes=50_000, time_limit_s=120.0),
    )
    base = Snapshot(tenant, invoices=tuple(invoices), config=cfg)
    return Scenario(
        "bpi2019",
        base,
        picked,
        {
            "file": _rel(path),
            "sha256": sha256_file(path),
            "po_items": n_items,
            "selection": f"first {max_items} PO items (file order) with invoice receipts; "
            f"first {max_changes} change events in event-time order",
            "changes_available": len(changes),
        },
    )


def _rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(repo_root().resolve()).as_posix()
    except ValueError:
        return str(p)


def _bpi_input() -> Path | None:
    from jettae.sources.bpi2019 import download

    m = download.load_manifest()
    conv = (m.get("converted") or {}).get("path")
    if conv and (repo_root() / conv).exists():
        return repo_root() / conv
    return download.local_log_path()


def run(
    *,
    ftc_rows: Path | None = None,
    bpi: Path | None = None,
    max_items: int = 300,
    max_changes: int = 300,
    out: Path | None = None,
    md: Path | None = None,
) -> dict[str, Any]:
    ran: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    default_ftc = data_dir() / "seeds" / "ftc_rows.csv"
    ftc_path = ftc_rows or default_ftc
    registered_bpi = _bpi_input()
    # Inputs other than the manifest-tracked seed / the registered real log are "ad hoc":
    # their numbers never go into docs/eval_results.md or the tracked results file.
    adhoc: list[str] = []
    if ftc_rows is not None and ftc_rows.resolve() != default_ftc.resolve():
        adhoc.append(f"ftc_rows={ftc_rows}")
    if bpi is not None and (registered_bpi is None or bpi.resolve() != registered_bpi.resolve()):
        adhoc.append(f"bpi={bpi}")
    try:
        ran.append(run_scenario(ftc_scenario(ftc_path)))
    except ScenarioUnavailable as e:
        skipped.append({"scenario": "ftc_rows", "reason": str(e)})
    bpi_path = bpi or registered_bpi
    if bpi_path is None:
        skipped.append(
            {
                "scenario": "bpi2019",
                "reason": "real BPI 2019 log not available (see data/manifests/bpi2019.json)",
            }
        )
    else:
        try:
            ran.append(run_scenario(bpi_scenario(bpi_path, max_items, max_changes)))
        except ScenarioUnavailable as e:
            skipped.append({"scenario": "bpi2019", "reason": str(e)})
    for s in ran:
        s["steps"] = s["steps"][:2000]
    res: dict[str, Any] = {
        "eval": "E6",
        "kind": "full vs incremental recompute equality on real records and real change order",
        "run": {"at": utc_now_iso(), "command": CMD},
        "scenarios": ran,
        "skipped": skipped,
        "all_equal": all(s["all_equal"] for s in ran) if ran else None,
        "adhoc_inputs": adhoc,
        "bpi_provenance_warning": _bpi_warning(bpi_path, registered_bpi),
    }
    if adhoc:
        res["kind"] = "AD-HOC INPUT (not the tracked real data): " + res["kind"]
        write_json(out or results_dir() / "recompute_eval_adhoc.json", res)
        if md is not None:
            upsert_md_section(md, "E6", render_md(res))
        return res
    write_json(out or results_dir() / "recompute_eval.json", res)
    upsert_md_section(md or eval_results_md(), "E6", render_md(res))
    return res


def _bpi_warning(used: Path | None, registered: Path | None) -> str | None:
    if used is None or registered is None or used.resolve() != registered.resolve():
        return None
    from jettae.sources.bpi2019 import download

    return download.provenance_warning()


def render_md(res: dict[str, Any]) -> str:
    lines = [
        "## E6 — 재계산 동등성 (full vs incremental, real records)",
        "",
        f"Run {res['run']['at']} · `{res['run']['command']}` · details: "
        "`data/results/recompute_eval.json`",
        "",
    ]
    if res.get("adhoc_inputs"):
        lines.insert(
            2,
            "**AD-HOC INPUT** (" + ", ".join(res["adhoc_inputs"]) + "): not the tracked real "
            "data; not part of the evaluation results.\n",
        )
    if res.get("bpi_provenance_warning"):
        lines.append(f"**BPI input provenance: {res['bpi_provenance_warning']}**")
        lines.append("")
    if not res["scenarios"]:
        lines.append("**Not run on real data**: no real input was available.")
    else:
        lines += [
            "| scenario | changes | mismatching steps | fallback full | recomputed decisions "
            "/ full | incremental ms / full ms |",
            "|---|---|---|---|---|---|",
        ]
        for s in res["scenarios"]:
            lines.append(
                f"| {s['scenario']} ({s['provenance'].get('file')}) | {s['changes']} | "
                f"{s['mismatching_steps']} | {s['fallback_full_steps']} | "
                f"{s['recomputed_decisions_total']} / {s['full_decisions_total']} "
                f"({s['recomputed_decision_share']}) | {s['incremental_ms_total']} / "
                f"{s['full_ms_total']} |"
            )
    for sk in res["skipped"]:
        lines.append(f"- skipped `{sk['scenario']}`: {sk['reason']}")
    return "\n".join(lines)


def run_cli(
    ftc_rows: Path | None = None,
    bpi: Path | None = None,
    max_items: int = 300,
    max_changes: int = 300,
) -> None:
    """E6: 실제 기록에 실제 순서의 변경을 적용해 전체 vs 선택적 재계산 결과를 비교."""
    res = run(ftc_rows=ftc_rows, bpi=bpi, max_items=max_items, max_changes=max_changes)
    print(render_md(res))
    if res["all_equal"] is False:
        raise SystemExit(1)


def _argparse_main() -> None:  # pragma: no cover - ``python -m jettae.evals.recompute_eval``
    import argparse

    ap = argparse.ArgumentParser(description="E6 full vs incremental recompute on real data")
    ap.add_argument("--ftc-rows", type=Path)
    ap.add_argument("--bpi", type=Path)
    ap.add_argument("--max-items", type=int, default=300)
    ap.add_argument("--max-changes", type=int, default=300)
    a = ap.parse_args()
    run_cli(a.ftc_rows, a.bpi, a.max_items, a.max_changes)


if __name__ == "__main__":  # pragma: no cover
    _argparse_main()
