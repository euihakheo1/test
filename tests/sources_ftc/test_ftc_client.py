"""DRF client behaviour with a mock transport (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from ftc_test_helpers import fixture_bytes

from jettae.sources.ftc.client import (
    DrfClient,
    DrfError,
    check_decision_xml,
    parse_search,
    public_url,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _client(handler, tmp_path: Path, clock: FakeClock, **kw) -> DrfClient:  # type: ignore[no-untyped-def]
    return DrfClient(
        oc="secret-oc",
        cache_dir=tmp_path,
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock.now,
        **kw,
    )


def test_fetch_decision_caches_and_redacts_oc(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, content=fixture_bytes("f16947.xml"))

    clock = FakeClock()
    with _client(handler, tmp_path, clock) as c:
        first = c.fetch_decision("16947")
        second = c.fetch_decision("16947")
    assert len(calls) == 1
    assert calls[0].url.params["OC"] == "secret-oc"
    assert not first.from_cache and second.from_cache
    assert first.sha256 == second.sha256
    assert "secret-oc" not in first.url and "OC={OC}" in first.url
    meta = json.loads((tmp_path / "xml" / "16947.m.json").read_text(encoding="utf-8"))
    assert "secret-oc" not in json.dumps(meta)


def test_retries_on_5xx_then_succeeds(tmp_path: Path) -> None:
    responses = [
        httpx.Response(500),
        httpx.Response(503),
        httpx.Response(200, content=b"\x89PNG.."),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    clock = FakeClock()
    with _client(handler, tmp_path, clock, backoff=1.0) as c:
        got = c.fetch_image("123")
    assert got.path.name == "123.png"
    assert c.network_requests == 3
    assert clock.sleeps.count(1.0) >= 1 and 2.0 in clock.sleeps  # exponential backoff


def test_gives_up_after_max_retries(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>error</html>")

    with _client(handler, tmp_path, FakeClock(), max_retries=2) as c, pytest.raises(DrfError):
        c.fetch_decision("99999999")


def test_rate_limit_spaces_requests(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG..")

    clock = FakeClock()
    with _client(handler, tmp_path, clock, min_interval=1.5) as c:
        c.fetch_image("1")
        c.fetch_image("2")
        c.fetch_image("3")
    assert clock.sleeps == [1.5, 1.5]


def test_error_payloads_are_rejected(tmp_path: Path) -> None:
    bad = (
        '<?xml version="1.0" encoding="UTF-8"?><Response><result>사용자 정보 검증에 실패하였습니다.'
        "</result></Response>"
    ).encode()
    with pytest.raises(DrfError, match="사용자 정보 검증"):
        check_decision_xml(bad, "19065")
    with pytest.raises(DrfError, match="response is for id"):
        check_decision_xml(fixture_bytes("f16947.xml"), "19065")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=bad)

    with _client(handler, tmp_path, FakeClock()) as c, pytest.raises(DrfError):
        c.fetch_decision("19065")
    assert not (tmp_path / "xml" / "19065.xml").exists()  # errors are not cached


def test_offline_mode_never_hits_network(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("network used in offline mode")

    with _client(handler, tmp_path, FakeClock(), offline=True) as c, pytest.raises(DrfError):
        c.fetch_decision("16947")


def test_search_paging_and_single_hit(tmp_path: Path) -> None:
    def page(n: int, total: int, items: list[dict]) -> bytes:  # type: ignore[type-arg]
        return json.dumps(
            {"Ftc": {"totalCnt": str(total), "page": str(n), "ftc": items}}, ensure_ascii=False
        ).encode()

    def item(i: int) -> dict[str, str]:
        return {
            "결정문일련번호": str(i),
            "사건번호": f"C{i}",
            "사건명": f"하도급 {i}",
            "결정번호": "의 결  제1호",
            "결정일자": "2026.1.1.",
            "문서유형": "의결서",
            "회의종류": "제 1 소 회 의",
        }

    def handler(req: httpx.Request) -> httpx.Response:
        p = int(req.url.params["page"])
        items = [item(1), item(2)] if p == 1 else [item(3)]
        return httpx.Response(200, content=page(p, 3, items))

    with _client(handler, tmp_path, FakeClock()) as c:
        total, hits = c.search_all("하도급", display=2)
    assert total == 3 and [h.decision_id for h in hits] == ["1", "2", "3"]
    assert hits[0].meeting == "제 1 소 회 의"
    single = parse_search(
        json.dumps({"Ftc": {"totalCnt": "1", "ftc": item(9)}}, ensure_ascii=False).encode()
    )
    assert [h.decision_id for h in single.hits] == ["9"]
    with pytest.raises(DrfError):
        parse_search(b"<html>maintenance</html>")


def test_public_url_redacts_oc() -> None:
    url = public_url("https://www.law.go.kr/DRF/lawService.do", {"OC": "me", "ID": "1"})
    assert url == "https://www.law.go.kr/DRF/lawService.do?OC={OC}&ID=1"


def test_invalid_ids_rejected(tmp_path: Path) -> None:
    with _client(lambda r: httpx.Response(200), tmp_path, FakeClock()) as c:
        with pytest.raises(ValueError):
            c.fetch_decision("../etc")
        with pytest.raises(ValueError):
            c.fetch_image("1;2")
