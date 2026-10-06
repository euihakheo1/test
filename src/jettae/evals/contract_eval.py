"""E5 — payment-term / start-date clause extraction from the FTC standard contracts.

Input: documents recorded in ``data/manifests/contract.json`` (real downloads only).
Output: every extracted clause with its span, plus *consistency* checks against the
statutory rule registry (직매입: 상품수령일 기준, 특약매입·위수탁: 월 판매마감일 기준, and the
statutory day counts). There are no independent gold labels, so this is not an accuracy
figure. A standard contract's wording says nothing about whether a real trade complies.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jettae.rules.kr_retail import RULE_CONSIGNMENT, RULE_DIRECT, builtin_registry
from jettae.rules.registry import RuleRegistry
from jettae.sources import (
    eval_results_md,
    repo_root,
    results_dir,
    sha256_file,
    upsert_md_section,
    utc_now_iso,
    write_json,
)
from jettae.sources.contract import ftc_board
from jettae.sources.contract.clauses import Extraction, contract_type, extract
from jettae.sources.contract.textx import UnsupportedDocument, read_document

RULE_FOR = {"direct": RULE_DIRECT, "consignment": RULE_CONSIGNMENT}
CMD = "uv run jettae sources law contract-extract"
# broader keyword filter used only as a coverage self-check (not a gold standard)
BROAD_TERM = re.compile(r"일\s*(?:이내|안에|내에|까지)")
BROAD_PAY = re.compile(r"지급|대금")


def expectations(reg: RuleRegistry | None = None) -> dict[str, dict[str, Any]]:
    reg = reg or builtin_registry()
    out = {}
    for t, rid in RULE_FOR.items():
        active = [rv for rv in reg.versions(rid) if rv.effective_from is not None]
        rv = max(active, key=lambda r: (r.effective_from, r.version))
        out[t] = {
            "rule": rv.key,
            "base": rv.params["base"],
            "term_days": int(rv.params["term_days"]),
        }
    return out


def assess(title: str, ex: Extraction, exp: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Consistency of extracted clauses with the statutory expectation for the title."""
    ctype = contract_type(title)
    pay = ex.payment_clauses()
    notes = ex.notes()
    res: dict[str, Any] = {
        "contract_type": ctype,
        "payment_clauses": len(pay),
        "drafting_notes": len(notes),
        "payment_clause_found": bool(pay),
    }
    if ctype is None:
        res["expected"] = None
        return res
    e = exp[ctype]
    res["expected"] = e
    with_base = [c for c in pay if c.base_kind is not None]
    res["base_found"] = bool(with_base)
    res["base_consistent"] = bool(with_base) and all(c.base_kind == e["base"] for c in with_base)
    res["base_kinds"] = sorted({c.base_kind for c in with_base if c.base_kind})
    res["base_wording"] = sorted({c.base_text for c in with_base if c.base_text})
    if with_base and not res["base_consistent"]:
        res["base_note"] = (
            f"기산점 표현이 규칙의 기준({e['base']})과 다름: "
            + ", ".join(res["base_wording"])
            + " — 같은 날인지 확인이 필요한 조건"
        )
    terms = [c for c in pay if c.term_text is not None]
    res["term_blank"] = any(c.term_blank for c in terms)
    days = sorted({c.term_days for c in terms if c.term_days is not None})
    res["term_days_stated"] = days
    res["term_within_statutory"] = all(d <= e["term_days"] for d in days) if days else None
    note_days = sorted({c.term_days for c in notes if c.term_days is not None})
    res["note_term_days"] = note_days
    res["note_matches_statute"] = (note_days == [e["term_days"]]) if note_days else None
    return res


def evaluate_documents(docs: list[dict[str, Any]], root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    exp = expectations()
    rows: list[dict[str, Any]] = []
    for d in docs:
        row: dict[str, Any] = {
            k: d.get(k) for k in ("ntt", "title", "registered", "format", "sha256", "post_url")
        }
        if not d.get("ok") or not d.get("path"):
            row.update(status="not_downloaded", error=d.get("error"))
            rows.append(row)
            continue
        p = root / d["path"]
        if not p.exists():
            row.update(status="file_missing", error=f"{d['path']} not on disk; run contract-fetch")
            rows.append(row)
            continue
        if d.get("sha256") and sha256_file(p) != d["sha256"]:
            row.update(status="hash_mismatch", error="file on disk differs from manifest")
            rows.append(row)
            continue
        try:
            kind, paras = read_document(p.read_bytes(), p.name)
        except UnsupportedDocument as e:
            row.update(status="unsupported", error=str(e))
            rows.append(row)
            continue
        ex = extract(paras)
        broad = {
            (p.section, p.index)
            for p in paras
            if BROAD_TERM.search(p.text) and BROAD_PAY.search(p.text)
        }
        got = {(c.locator["section"], c.locator["paragraph"]) for c in ex.clauses}
        row.update(
            broad_candidates=len(broad),
            broad_candidates_extracted=len(broad & got),
            status="extracted",
            parsed_as=kind,
            paragraphs=ex.paragraphs,
            articles=len(ex.articles),
            clauses=[c.to_json() for c in ex.clauses],
            assessment=assess(str(d.get("title", "")), ex, exp),
        )
        rows.append(row)
    ext = [r for r in rows if r["status"] == "extracted"]
    typed = [r for r in ext if r["assessment"]["contract_type"]]

    def count(key: str) -> int:
        return sum(1 for r in typed if r["assessment"].get(key) is True)

    return {
        "eval": "E5",
        "kind": "rule-based extraction + consistency with the statutory registry "
        "(no independent gold labels; not an accuracy figure)",
        "expectations": exp,
        "summary": {
            "documents": len(rows),
            "extracted": len(ext),
            "failed": len(rows) - len(ext),
            "with_payment_clause": sum(1 for r in ext if r["assessment"]["payment_clause_found"]),
            "typed_documents": len(typed),
            "base_consistent": count("base_consistent"),
            "term_blank_in_form": count("term_blank"),
            "term_within_statutory": count("term_within_statutory"),
            "note_matches_statute": count("note_matches_statute"),
            "broad_filter_paragraphs": sum(r.get("broad_candidates", 0) for r in ext),
            "broad_filter_paragraphs_extracted": sum(
                r.get("broad_candidates_extracted", 0) for r in ext
            ),
        },
        "documents": rows,
    }


def run(out: Path | None = None, md: Path | None = None) -> dict[str, Any]:
    m = ftc_board.load_manifest()
    if m is None:
        raise FileNotFoundError(
            "data/manifests/contract.json missing: run `uv run jettae sources law contract-fetch`"
        )
    res = evaluate_documents(m.get("documents", []))
    res["run"] = {
        "at": utc_now_iso(),
        "manifest": "data/manifests/contract.json",
        "manifest_fetched_at": m.get("fetched_at"),
        "command": CMD,
    }
    write_json(out or results_dir() / "contract_eval.json", res)
    upsert_md_section(md or eval_results_md(), "E5", render_md(res))
    return res


def render_md(res: dict[str, Any]) -> str:
    s = res["summary"]
    lines = [
        "## E5 — 지급기한·기산점 조항 추출 (공정위 표준거래계약서)",
        "",
        f"Run {res['run']['at']} · `{res['run']['command']}` · details: "
        "`data/results/contract_eval.json`",
        "",
        "Rule-based extraction from the real downloaded forms; checks are *consistency with the "
        "statutory rule registry*, not accuracy against independent labels.",
        "",
        f"Documents {s['documents']} · extracted {s['extracted']} · failed {s['failed']} · "
        f"with payment clause {s['with_payment_clause']} · start date consistent "
        f"{s['base_consistent']}/{s['typed_documents']} · period left blank in form "
        f"{s['term_blank_in_form']}/{s['typed_documents']} · drafting note equals statutory days "
        f"{s['note_matches_statute']}/{s['typed_documents']}",
        "",
        f"Coverage self-check: of {s['broad_filter_paragraphs']} paragraphs matching a broader "
        "keyword filter ('N일 이내/안에/까지' + 지급/대금), "
        f"{s['broad_filter_paragraphs_extracted']} were extracted.",
        "",
        "| 계약서 | 형태 | 조항 | 기산점 | 기한 | 작성 안내(※) |",
        "|---|---|---|---|---|---|",
    ]
    for r in res["documents"]:
        if r["status"] != "extracted":
            lines.append(f"| {r.get('title')} | - | {r['status']}: {r.get('error')} | | | |")
            continue
        a = r["assessment"]
        pays = [c for c in r["clauses"] if c["kind"] == "clause"]
        c0 = pays[0] if pays else None
        art = f"제{c0['article_no']}조" if c0 and c0["article_no"] else "-"
        base = c0["base_text"] if c0 and c0["base_text"] else "-"
        term = c0["term_text"] if c0 and c0["term_text"] else "-"
        nd = ",".join(str(x) for x in a.get("note_term_days", []))
        note_days = f"{nd}일" if nd else "-"
        if a.get("base_note"):
            base += f" ⚠ {a['base_note']}"
        lines.append(
            f"| {r['title']} ({r.get('registered')}) | {a['contract_type']} | {art} "
            f"(문단 {c0['locator']['paragraph'] if c0 else '-'}) | {base} | {term} "
            f"| {note_days} |"
        )
    return "\n".join(lines)


def run_cli() -> None:
    """E5: 공정위 표준거래계약서에서 지급기한·기산점 조항 추출(원문 위치 포함)."""
    try:
        res = run()
    except FileNotFoundError as e:
        print(e)
        raise SystemExit(3) from None
    print(render_md(res))
