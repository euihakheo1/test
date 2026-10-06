"""Collection pipeline: search -> fetch decision XML -> parse -> manifests / case facts / images.

Outputs (paths relative to the data dir):
- ``raw/ftc/xml/<id>.xml`` (+ ``.meta.json``) — cached DRF responses (gitignored)
- ``raw/ftc/img/<flSeq>.png`` — table images (gitignored)
- ``manifests/ftc.json`` — queries, decision ids, fetch time, sha256, URL (OC redacted), status
- ``manifests/ftc_images.json`` — downloaded table images with caption/score/sha256
- ``seeds/ftc_case_facts.jsonl`` — case-level regex facts with provenance (one per line)
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jettae.sources.ftc.client import DEFAULT_OC, DrfClient, DrfError, SearchHit
from jettae.sources.ftc.extract import extract_case_facts, summarize
from jettae.sources.ftc.parse import FtcDecision, delay_table_score, parse_decision
from jettae.sources.ftc.paths import data_dir, long_path, manifest_dir, seeds_dir

LICENSE_NOTE = (
    "공정거래위원회 의결서 원문(국가법령정보센터 Open API, target=ftc). 이용 조건은 국가법령정보 "
    "공동활용 이용약관을 따름(조건 세부는 이 저장소에서 별도 검증하지 않음)."
)


@dataclass(frozen=True)
class QuerySpec:
    name: str
    query: str
    body: bool  # False: 사건명 검색(search=1), True: 본문 검색(search=2)
    cap: int | None = None
    title_filter: str | None = None  # keep only hits whose 사건명 contains this text

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "query": self.query,
            "search": "본문(search=2)" if self.body else "사건명(search=1)",
            "cap": self.cap,
            "title_filter": self.title_filter,
        }


DEFAULT_QUERIES: tuple[QuerySpec, ...] = (
    QuerySpec("large_retail", "대규모유통", body=False),
    QuerySpec(
        "subcontract_delay_interest",
        "하도급대금 지연이자",
        body=True,
        cap=200,
        title_filter="하도급",
    ),
)


@dataclass
class FetchReport:
    queries: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    network_requests: int = 0

    @property
    def ok(self) -> list[dict[str, Any]]:
        return [d for d in self.decisions if d["status"] == "ok"]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(data_dir().resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def _write_json(path: Path, obj: Any) -> None:
    long_path(path.parent).mkdir(parents=True, exist_ok=True)
    tmp = long_path(path.with_name(path.name + ".part"))
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(long_path(path))


def decision_summary(dec: FtcDecision) -> dict[str, Any]:
    scored = [(t, *delay_table_score(t)) for t in dec.tables]
    return {
        "n_tables": len(dec.tables),
        "n_tables_unlinked": sum(1 for t in dec.tables if t.flseq is None),
        "n_delay_tables": sum(1 for _t, s, _r in scored if s >= 1),
        "n_footnotes": len(dec.footnotes),
        "n_inline_footnotes": sum(1 for f in dec.footnotes if f.inline),
        "sections": sorted(k for k in dec.sections if not k.startswith("각주:")),
    }


def collect_hits(
    client: DrfClient, queries: Iterable[QuerySpec], *, refresh: bool = False
) -> tuple[list[dict[str, Any]], dict[str, tuple[SearchHit, list[str]]]]:
    qlog: list[dict[str, Any]] = []
    hits: dict[str, tuple[SearchHit, list[str]]] = {}
    for q in queries:
        total, found = client.search_all(
            q.query, body=q.body, cap=None if q.title_filter else q.cap, refresh=refresh
        )
        if q.title_filter:
            found = [h for h in found if q.title_filter in h.title]
            if q.cap is not None:
                found = found[: q.cap]
        qlog.append({**q.to_json(), "total": total, "selected": len(found), "run_at": _now()})
        for h in found:
            if h.decision_id in hits:
                hits[h.decision_id][1].append(q.name)
            else:
                hits[h.decision_id] = (h, [q.name])
    return qlog, hits


def fetch(
    client: DrfClient,
    *,
    queries: Iterable[QuerySpec] = DEFAULT_QUERIES,
    limit: int | None = None,
    refresh: bool = False,
    progress: Callable[[str], None] | None = None,
    write: bool = True,
) -> FetchReport:
    """Search, fetch every selected decision XML (cached), parse it, write manifest + facts."""
    rep = FetchReport()
    qlog, hits = collect_hits(client, queries, refresh=refresh)
    rep.queries = qlog
    selected = list(hits.values())
    if limit is not None:
        selected = selected[:limit]
    facts_out: list[dict[str, Any]] = []
    for n, (hit, qnames) in enumerate(selected, 1):
        rec: dict[str, Any] = {
            "decision_id": hit.decision_id,
            "case_no": hit.case_no,
            "title": hit.title,
            "decision_no": hit.decision_no,
            "decision_date": hit.decision_date,
            "doc_type": hit.doc_type,
            "queries": qnames,
        }
        try:
            got = client.fetch_decision(hit.decision_id, refresh=refresh)
            dec = parse_decision(got.read_bytes())
            facts = extract_case_facts(dec)
        except (DrfError, ValueError, OSError) as e:
            rec.update(status="failed", error=str(e)[:300])
            rep.failures.append(rec)
            rep.decisions.append(rec)
            if progress:
                progress(f"[{n}/{len(selected)}] {hit.decision_id} FAILED: {e}")
            continue
        rec.update(
            status="ok",
            url=got.url,
            sha256=got.sha256,
            size=got.size,
            fetched_at=got.fetched_at,
            from_cache=got.from_cache,
            path=_rel(got.path),
            **decision_summary(dec),
            facts=summarize(facts),
        )
        rep.decisions.append(rec)
        for f in facts:
            facts_out.append({**f.to_json(), "doc_sha256": got.sha256})
        if progress:
            src = "cache" if got.from_cache else "net"
            progress(
                f"[{n}/{len(selected)}] {hit.decision_id} {hit.case_no} ({src}) "
                f"tables={rec['n_tables']} delay_tables={rec['n_delay_tables']} facts={len(facts)}"
            )
    rep.network_requests = client.network_requests
    if write:
        write_manifest(rep, client)
        merge_facts(facts_out, {d["decision_id"] for d in rep.decisions})
    return rep


def write_manifest(rep: FetchReport, client: DrfClient) -> Path:
    """Write ``manifests/ftc.json``; decisions from earlier runs that this run did not touch
    (e.g. a ``--limit`` run after a full one) are kept, with ``in_last_run: false``."""
    path = manifest_dir() / "ftc.json"
    this_run = {d["decision_id"] for d in rep.decisions}
    kept: list[dict[str, Any]] = []
    if long_path(path).exists():
        try:
            old = json.loads(long_path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            old = {}
        kept = [
            {**d, "in_last_run": False}
            for d in old.get("decisions", [])
            if d.get("decision_id") not in this_run
        ]
    decisions = [{**d, "in_last_run": True} for d in rep.decisions] + kept
    _write_json(
        path,
        {
            "source": "ftc",
            "provider": "법제처 국가법령정보 공동활용 DRF Open API (target=ftc)",
            "endpoints": {
                "search": "https://www.law.go.kr/DRF/lawSearch.do",
                "decision": "https://www.law.go.kr/DRF/lawService.do",
                "file": "https://www.law.go.kr/LSW/flDownload.do",
            },
            "oc": "sample key 'test' (development only)"
            if client.oc == DEFAULT_OC
            else "custom OC from JETTAE_DRF_OC (value not recorded)",
            "license": LICENSE_NOTE,
            "generated_at": _now(),
            "queries": rep.queries,
            "counts": {
                "selected_this_run": len(rep.decisions),
                "ok_this_run": len(rep.ok),
                "failed_this_run": len(rep.failures),
                "network_requests_this_run": rep.network_requests,
                "decisions_total": len(decisions),
                "ok_total": sum(1 for d in decisions if d.get("status") == "ok"),
            },
            "decisions": decisions,
        },
    )
    return path


def merge_facts(rows: list[dict[str, Any]], replaced_ids: set[str]) -> Path:
    """Replace the facts of ``replaced_ids`` in the seeds file, keep all other decisions."""
    path = seeds_dir() / "ftc_case_facts.jsonl"
    kept: list[dict[str, Any]] = []
    if long_path(path).exists():
        with long_path(path).open(encoding="utf-8") as fh:
            kept = [
                r
                for r in (json.loads(line) for line in fh if line.strip())
                if r["decision_id"] not in replaced_ids
            ]
    return write_facts(kept + rows)


def write_facts(rows: list[dict[str, Any]]) -> Path:
    path = seeds_dir() / "ftc_case_facts.jsonl"
    long_path(path.parent).mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (int(r["decision_id"]), r["section"], r["char_start"]))
    with long_path(path).open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def load_manifest() -> dict[str, Any]:
    path = manifest_dir() / "ftc.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: run `jettae sources ftc fetch` first")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def download_images(
    client: DrfClient,
    *,
    min_score: int = 2,
    decision_ids: Iterable[str] | None = None,
    limit: int | None = None,
    refresh: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Download images of tables that look like delay/interest tables, for fetched decisions."""
    man = load_manifest()
    wanted = set(decision_ids) if decision_ids else None
    images: list[dict[str, Any]] = []
    skipped_unlinked = 0
    for d in man["decisions"]:
        if d.get("status") != "ok" or (wanted is not None and d["decision_id"] not in wanted):
            continue
        dec = parse_decision(client.fetch_decision(d["decision_id"]).read_bytes())
        for t in dec.tables:
            score, reason = delay_table_score(t)
            if score < min_score:
                continue
            base = {
                "decision_id": dec.decision_id,
                "case_no": dec.case_no,
                "title": dec.title,
                "section": t.section,
                "image_index": t.index,
                "alt": t.alt,
                "table_label": t.label,
                "caption": t.caption,
                "unit_note": t.unit_note,
                "continuation": t.continuation,
                "score": score,
                "reason": reason,
            }
            if t.flseq is None:
                skipped_unlinked += 1
                images.append({**base, "flseq": None, "status": "unlinked", "src": t.src})
                continue
            if limit is not None and sum(1 for i in images if i.get("status") == "ok") >= limit:
                break
            try:
                got = client.fetch_image(t.flseq, refresh=refresh)
            except DrfError as e:
                images.append({**base, "flseq": t.flseq, "status": "failed", "error": str(e)})
                if progress:
                    progress(f"{dec.decision_id} flSeq={t.flseq} FAILED {e}")
                continue
            images.append(
                {
                    **base,
                    "flseq": t.flseq,
                    "status": "ok",
                    "url": got.url,
                    "sha256": got.sha256,
                    "size": got.size,
                    "fetched_at": got.fetched_at,
                    "path": _rel(got.path),
                    "from_cache": got.from_cache,
                }
            )
            if progress:
                progress(
                    f"{dec.decision_id} {t.label or '-'} flSeq={t.flseq} "
                    f"({'cache' if got.from_cache else 'net'}) {t.caption or reason}"
                )
    out = {
        "source": "ftc_images",
        "generated_at": _now(),
        "min_score": min_score,
        "selection_rule": (
            "score 2: <표 N> caption contains 지연/이자 together with 지급/이자/대금/공탁/내역, "
            "or 공탁, or 지급+대금; score 1: uncaptioned image right after text on "
            "지연이자/법정지급기한, or 별지 near 지연/이자"
        ),
        "counts": {
            "ok": sum(1 for i in images if i["status"] == "ok"),
            "failed": sum(1 for i in images if i["status"] == "failed"),
            "unlinked": skipped_unlinked,
            "decisions": len({i["decision_id"] for i in images}),
        },
        "images": images,
    }
    _write_json(manifest_dir() / "ftc_images.json", out)
    return out
