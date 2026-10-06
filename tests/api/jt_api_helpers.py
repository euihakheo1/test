"""Hand-written helpers for API / DB tests (not evaluation data).

``FakeIngest`` is a test double for the ingestion port: it understands a tiny CSV layout
used only in these tests. The real parsers (``jettae.ingest``) are exercised through
``ModuleIngest`` in ``tests/integration`` and ``test_api_real_ingest``."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

from jettae.app.contracts import MappingRequest, ParseResult, RowCounts
from jettae.domain.models import BankTxn, DocumentVersion, Fact, Invoice, SourceSpan
from jettae.domain.money import Money
from jettae.domain.status import DocumentStatus, TradeType

START = datetime(2025, 11, 1, 0, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"


class FakeClock:
    def __init__(self, start: datetime = START) -> None:
        self._now = start
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now = self._now + timedelta(seconds=seconds)


LEDGER_CSV = (
    "entity,id,counterparty,amount,date\n"
    "invoice,I1,가나유통,10000000,2025-08-07\n"
    "bank_txn,T1,(주)가나유통,10000000,2025-10-20\n"
)


class FakeIngest:
    """Rows ``entity,id,counterparty,amount,date``; ``needs_mapping`` when the header differs."""

    def __init__(self) -> None:
        self.calls = 0

    def suggest_mapping(self, doc: DocumentVersion, content: bytes) -> Mapping[str, Any]:
        header = content.decode("utf-8").splitlines()[0].split(",")
        return {"columns": header, "fields": {h: i for i, h in enumerate(header)}}

    def parse(
        self, doc: DocumentVersion, content: bytes, mapping: MappingRequest | None
    ) -> ParseResult:
        self.calls += 1
        text = content.decode("utf-8")
        lines = text.splitlines()
        known = lines[0].split(",") == ["entity", "id", "counterparty", "amount", "date"]
        if not known and mapping is None:
            return ParseResult(
                status=DocumentStatus.NEEDS_MAPPING,
                text=text,
                reason="unknown header",
                suggestion=self.suggest_mapping(doc, content),
            )
        facts: list[Fact] = []
        records: list[Any] = []
        for n, line in enumerate(lines[1:], start=2):
            entity, rid, cp, amount, d = line.split(",")
            fid = f"f-{doc.id}-{rid}"
            facts.append(
                Fact(
                    id=fid,
                    tenant_id=doc.tenant_id,
                    kind="amount",
                    value=int(amount),
                    span=SourceSpan(doc.id, {"row": n, "col": "D"}, amount),
                    extractor="fake-test-ingest",
                    observed_at=START,
                    subject_id=rid,
                )
            )
            if entity == "invoice":
                records.append(
                    Invoice(
                        id=rid,
                        tenant_id=doc.tenant_id,
                        counterparty=cp,
                        amount=Money(int(amount)),
                        trade_type=TradeType.DIRECT,
                        goods_received_date=date.fromisoformat(d),
                        facts=(fid,),
                    )
                )
            else:
                records.append(
                    BankTxn(
                        id=rid,
                        tenant_id=doc.tenant_id,
                        booked_date=date.fromisoformat(d),
                        amount=Money(int(amount)),
                        counterparty=cp,
                        facts=(fid,),
                    )
                )
        rows = len(lines) - 1
        return ParseResult(
            status=DocumentStatus.PARSED,
            facts=tuple(facts),
            records=tuple(records),
            text=text,
            counts=RowCounts(
                tables_recognized=1, source_rows=rows, applied_rows=rows, excluded_rows=0
            ),
        )


def bearer_from_cookies(client: Any, r: Any) -> dict[str, str]:
    """Turn the session cookie of a login/signup response into a Bearer header and drop the
    client's cookie jar, so tests that act as several users on one TestClient never send a
    leftover cookie. The cookie flow itself is tested in ``test_auth_cookies.py``."""
    token = r.cookies.get("jt_access")
    assert token, "login/signup must set the jt_access cookie"
    assert "access_token" not in r.json() and "refresh_token" not in r.json()
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


def session_cookies(r: Any) -> dict[str, str]:
    """The three session cookies set by a login/signup/refresh response (only those set)."""
    return {k: v for k in ("jt_access", "jt_refresh", "jt_csrf") if (v := r.cookies.get(k))}


def cookie_header(cookies: dict[str, str], *, csrf: bool = True) -> dict[str, str]:
    """Explicit ``Cookie`` (+ ``X-CSRF-Token``) headers, for tests that must not depend on the
    TestClient cookie jar (several sessions, threads, replayed tokens)."""
    h = {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
    if csrf and "jt_csrf" in cookies:
        h["X-CSRF-Token"] = cookies["jt_csrf"]
    return h


def send(
    client: Any,
    method: str,
    url: str,
    cookies: dict[str, str],
    *,
    csrf: bool = True,
    headers: dict[str, str] | None = None,
    **kw: Any,
) -> Any:
    """One request carrying exactly ``cookies`` (httpx would replace an explicit ``Cookie``
    header with its jar, so the jar is emptied before and after)."""
    client.cookies.clear()
    try:
        return client.request(
            method, url, headers={**cookie_header(cookies, csrf=csrf), **(headers or {})}, **kw
        )
    finally:
        client.cookies.clear()


def login_session(
    client: Any, email: str, password: str = PASSWORD, tenant_id: str | None = None
) -> dict[str, str]:
    body: dict[str, Any] = {"email": email, "password": password}
    if tenant_id is not None:
        body["tenant_id"] = tenant_id
    r = client.post("/api/v1/auth/login", json=body)
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return session_cookies(r)


def signup(client: Any, email: str, tenant: str) -> dict[str, str]:
    r = client.post(
        "/api/v1/auth/signup", json={"email": email, "password": PASSWORD, "tenant_name": tenant}
    )
    assert r.status_code == 201, r.text
    return bearer_from_cookies(client, r)


def invoice_change(rid: str = "I1", amount: int = 10_000_000, cp: str = "가나유통") -> dict:
    return {
        "kind": "add",
        "entity": "invoice",
        "id": rid,
        "record": {
            "id": rid,
            "counterparty": cp,
            "amount": amount,
            "trade_type": "direct",
            "goods_received_date": "2025-08-07",
        },
    }


def txn_change(rid: str = "T1", amount: int = 10_000_000, d: str = "2025-10-20") -> dict:
    return {
        "kind": "add",
        "entity": "bank_txn",
        "id": rid,
        "record": {
            "id": rid,
            "booked_date": d,
            "amount": amount,
            "counterparty": "(주)가나유통",
        },
    }


def agreement_change(rid: str = "A1") -> dict:
    return {
        "kind": "add",
        "entity": "agreement",
        "id": rid,
        "record": {"id": rid, "counterparty": "가나유통", "rollover": True},
    }
