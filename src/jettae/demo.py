"""``jettae demo run``: a local demo built from real transcribed rows, never from invented data.

Input is ``data/seeds/ftc_rows.csv``: rows typed in from the table images of published
공정거래위원회 decisions (법제처 DRF). The transcription has not been checked by a person
(``verified=no``), so every output of the demo carries that label.

How the rows become upload files (no value is invented):
- settlement CSV: one line per transcribed row. 거래처 = the 피심인 named in the decision title
  (``data/manifests/ftc.json``), 정산금액 = the printed principal, 거래형태 from the table's deal
  type, and the statutory base date only when the table prints one. Rows of tables without a
  base-date column stay without it, so the engine shows them as 근거 부족 with required
  documents (it never substitutes another date).
- bank CSV: one deposit per row on the printed payment date for the printed principal.
- Skipped, with the reason counted: range rows (``a~b``, aggregated), rows printed in 천원
  (the exact won amount is not printed), rows without principal or payment date.

The demo uses the same path as the service: Alembic migration, the auth service for the demo
user, ``JettaeService.register_document`` + the DB job queue, and one worker pass. It runs only
with ``JETTAE_ENV=dev``: it creates an account with a password printed once, which must never
happen against a test or production database. The password is not written anywhere.
"""

from __future__ import annotations

import csv
import io
import json
import re
import secrets
import warnings
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer

from jettae.config import REPO_ROOT

SOURCE_LABEL = "공정위 의결서 표에서 옮긴 실제 사례, 전사 검증 전"
DEFAULT_ROWS = REPO_ROOT / "data" / "seeds" / "ftc_rows.csv"
DEFAULT_MANIFEST = REPO_ROOT / "data" / "manifests" / "ftc.json"

_TRADE_TEXT = {"direct": "직매입", "consignment": "특약매입", "subcontract": "하도급"}
# base_date_kind in the seed -> settlement column that holds that statutory base date
_BASE_COLUMN = {
    "goods_received_date": "상품수령일",
    "object_received_date": "상품수령일",  # 하도급: 목적물수령일 (same engine field)
    "sales_close_date": "판매마감일",
}
SETTLE_HEADER = ("거래처", "정산번호", "거래형태", "구분", "상품수령일", "판매마감일", "정산금액")
BANK_HEADER = ("거래일자", "적요", "기재내용", "입금자명", "입금액", "출금액")

app = typer.Typer(
    help="실제 공개 자료(공정위 의결서 전사 행)로 만드는 로컬 데모", no_args_is_help=True
)


class DemoError(RuntimeError):
    pass


@dataclass
class DemoFiles:
    settlement_csv: bytes
    bank_csv: bytes
    rows_used: int
    rows_without_base_date: int
    skipped: Counter[str] = field(default_factory=Counter)
    decisions: dict[str, str] = field(default_factory=dict)  # decision_id -> counterparty
    max_paid: date | None = None
    source_sha256: str = ""


def _counterparties(manifest: Path) -> dict[str, str]:
    """decision_id -> 피심인 name, taken from the decision title ("X의 ... 위반행위에 대한 건")."""
    data = json.loads(manifest.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for d in data.get("decisions", []):
        title = str(d.get("title") or "")
        m = re.match(r"\s*(.+?)의\s", title)
        if m:
            out[str(d.get("decision_id"))] = m.group(1).strip()
    return out


def _iso(v: str) -> date | None:
    try:
        return date.fromisoformat(v.strip())
    except ValueError:
        return None


def build_files(rows_path: Path = DEFAULT_ROWS, manifest: Path = DEFAULT_MANIFEST) -> DemoFiles:
    if not rows_path.is_file():
        raise DemoError(f"{rows_path} not found (data/seeds is part of the repository)")
    if not manifest.is_file():
        raise DemoError(f"{manifest} not found (data/manifests is part of the repository)")
    from jettae.domain.hashing import bytes_hash

    raw = rows_path.read_bytes()
    names = _counterparties(manifest)
    settle, bank = io.StringIO(), io.StringIO()
    sw, bw = csv.writer(settle, lineterminator="\n"), csv.writer(bank, lineterminator="\n")
    sw.writerow(SETTLE_HEADER)
    bw.writerow(BANK_HEADER)
    files = DemoFiles(b"", b"", 0, 0, source_sha256=bytes_hash(raw))
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
        principal = (r.get("principal_krw") or "").strip()
        paid = _iso(r.get("paid_date") or "")
        base = (r.get("base_date") or "").strip()
        if "~" in base or "~" in principal:
            files.skipped["range_row"] += 1
            continue
        if (r.get("unit_note") or "").strip() != "원":
            files.skipped["unit_not_won"] += 1
            continue
        if not principal.isdigit() or paid is None:
            files.skipped["no_principal_or_paid_date"] += 1
            continue
        did = (r.get("decision_id") or "").strip()
        cp = names.get(did)
        if cp is None:
            files.skipped["decision_not_in_manifest"] += 1
            continue
        files.decisions[did] = cp
        table = re.sub(r"\s+", "", r.get("table_label") or "")
        ref = f"{r['case_no']}-{table}-{int(r['row_idx']):02d}"
        cols = {"상품수령일": "", "판매마감일": ""}
        if base:
            col = _BASE_COLUMN.get((r.get("base_date_kind") or "").strip())
            if col is None or _iso(base) is None:
                files.skipped["unknown_base_date"] += 1
                continue
            cols[col] = base
        else:
            files.rows_without_base_date += 1
        trade = _TRADE_TEXT.get((r.get("deal_type") or "").strip(), "")
        sw.writerow([cp, ref, trade, "매입", cols["상품수령일"], cols["판매마감일"], principal])
        bw.writerow([paid.isoformat(), "입금", f"대금 {ref}", cp, principal, ""])
        files.rows_used += 1
        files.max_paid = max(files.max_paid or paid, paid)
    if files.rows_used == 0:
        raise DemoError("no usable rows in the seed file")
    # utf-8-sig: what spreadsheet programs write; the parser accepts it like cp949
    files.settlement_csv = settle.getvalue().encode("utf-8-sig")
    files.bank_csv = bank.getvalue().encode("utf-8-sig")
    return files


def _ensure_sqlite_dir(url: str) -> None:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)


def _decision_line(view: Any) -> str:
    """One summary line from the engine's ``due`` computation (never recomputed here)."""
    d = view.decision
    due = next((dict(c.outputs) for c in d.computations if c.name == "due"), {})
    head = f"  - {d.status.value:<22}"
    if not due or due.get("insufficient"):
        docs = ", ".join(d.required_documents) or "-"
        return f"{head} 계산 보류(미확인: {', '.join(d.missing) or '-'}) 필요 서류: {docs}"
    parts = []
    for v in due.get("variants") or []:
        interest = v.get("interest_total")
        parts.append(
            f"{v.get('label')}: 기한 {v.get('due_date')} 지연 {v.get('max_delay_days')}일 "
            f"지연이자 {interest if interest is not None else '미계산'}"
        )
    unresolved = due.get("unresolved") or []
    tail = f" (확인 필요: {', '.join(unresolved)})" if unresolved else ""
    return f"{head} 기준일 {due.get('base_date')} | " + "; ".join(parts) + tail


@app.command("run")
def run(
    out_dir: Annotated[
        Path | None,
        typer.Option("--out-dir", help="만든 업로드 파일(CSV)을 저장할 폴더(선택, 예: var/demo)"),
    ] = None,
    rows: Annotated[Path, typer.Option("--rows", help="전사 행 CSV")] = DEFAULT_ROWS,
    show: Annotated[int, typer.Option("--show", min=0, help="출력할 결과 줄 수")] = 8,
) -> None:
    """공정위 의결서 전사 행으로 데모 회사·사용자를 만들고 업로드→문서 읽기→분석을 실행한다."""
    from jettae.config import require_valid_environment

    st = require_valid_environment("db")
    if st.env != "dev":
        raise SystemExit(
            f"[jettae demo] JETTAE_ENV={st.env}: the demo creates an account with a printed "
            "password and runs only with JETTAE_ENV=dev"
        )
    try:
        files = build_files(rows)
    except DemoError as e:
        raise SystemExit(f"[jettae demo] {e}") from None

    from jettae.api.auth import AuthService
    from jettae.db import migrate
    from jettae.db.runtime import Runtime
    from jettae.domain.status import DocKind
    from jettae.worker import Worker

    _ensure_sqlite_dir(st.database_url)
    migrate.upgrade(st.database_url, "head", st.migrations_dir)
    rt = Runtime.build(st)
    try:
        email = f"demo-{secrets.token_hex(4)}@example.com"
        password = secrets.token_urlsafe(18)
        with warnings.catch_warnings():
            # the demo discards the issued tokens; an ephemeral dev JWT key is fine here
            warnings.simplefilter("ignore")
            pair = AuthService(rt.db, st, clock=rt.clock).signup(
                email, password, "데모 회사(공정위 의결서 전사 사례)"
            )
        tenant = pair.tenant_id
        jobs = []
        for name, content, kind in (
            ("demo_settlement.csv", files.settlement_csv, DocKind.SETTLEMENT),
            ("demo_bank.csv", files.bank_csv, DocKind.BANK),
        ):
            with rt.db.write(tenant):
                doc = rt.service.register_document(
                    tenant, filename=name, content=content, media_type="text/csv", kind=kind
                )
                jobs.append(
                    rt.queue.enqueue(
                        tenant, "ingest_document", {"doc_version_id": doc.id}, created_by=email
                    ).id
                )
        worker = Worker(rt, owner="jettae-demo", concurrency=1)
        worker.run_once()
        for jid in jobs:
            j = rt.queue.get(tenant, jid)
            if j is None or j.status.value != "succeeded":
                err = j.error if j is not None else None
                raise SystemExit(f"[jettae demo] document job {jid} did not succeed: {err}")
        as_of = files.max_paid or date.today()
        analysis = rt.queue.enqueue(
            tenant, "run_analysis", {"as_of": as_of.isoformat()}, created_by=email
        )
        worker.run_once()
        aj = rt.queue.get(tenant, analysis.id)
        if aj is None or aj.status.value != "succeeded":
            raise SystemExit(f"[jettae demo] analysis job failed: {aj.error if aj else None}")
        views = rt.service.list_decisions(tenant)
    finally:
        rt.close()

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "demo_settlement.csv").write_bytes(files.settlement_csv)
        (out_dir / "demo_bank.csv").write_bytes(files.bank_csv)

    result = aj.result or {}
    typer.echo(f"자료: {SOURCE_LABEL}")
    typer.echo(f"  원본 {rows.name} (sha256 {files.source_sha256[:16]}…)")
    typer.echo(
        f"  사용한 행 {files.rows_used}개"
        f"(기준일이 인쇄되지 않은 행 {files.rows_without_base_date}개 포함), "
        f"제외 {sum(files.skipped.values())}개 {dict(sorted(files.skipped.items()))}"
    )
    typer.echo(f"  거래처(피심인): {', '.join(sorted(set(files.decisions.values())))}")
    typer.echo(f"DB: {st.database_url}")
    typer.echo(f"분석 기준일(as_of): {as_of.isoformat()}")
    typer.echo(f"결과 {result.get('decisions')}건, 상태별 {result.get('by_status')}")
    # a few lines of each status, so both computed and withheld results are visible
    shown: Counter[str] = Counter()
    per_status = max(1, show // max(1, len({v.decision.status for v in views})))
    for v in views:
        if shown[v.decision.status.value] < per_status:
            shown[v.decision.status.value] += 1
            typer.echo(_decision_line(v))
    if out_dir is not None:
        typer.echo(f"업로드 파일: {out_dir / 'demo_settlement.csv'}, {out_dir / 'demo_bank.csv'}")
    typer.echo("")
    typer.echo("데모 로그인 (비밀번호는 지금 한 번만 출력하며 어디에도 저장하지 않습니다):")
    typer.echo(f"  이메일   {email}")
    typer.echo(f"  비밀번호 {password}")
    typer.echo("같은 JETTAE_DATABASE_URL로 API·worker·화면을 실행한 뒤 위 계정으로 로그인하세요.")
    typer.echo(
        "이 결과는 전사 검증 전 자료로 계산한 차이·지연일수·필요 서류이며, 법적 판단이 아닙니다."
    )
