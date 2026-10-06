"""DRF client + rule-parameter check on hand-written XML responses (not real fetches)."""

# ruff: noqa: E501  (fixtures quote statute / board HTML lines verbatim)

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from jettae.rules.kr_retail import RULE_DIRECT, RULE_INTEREST_RETAIL, default_registry
from jettae.rules.registry import RuleRegistry
from jettae.sources import redact_url
from jettae.sources.law import check, drf

ART8 = """<?xml version="1.0" encoding="UTF-8"?><법령><조문>
<조문단위><조문번호>8</조문번호><조문여부>조문</조문여부><조문제목>상품판매대금 등의 지급</조문제목>
<조문내용>제8조(상품판매대금 등의 지급)</조문내용>
<항><항내용>① 대규모유통업자는 다음 각 호의 어느 하나에 해당하는 경우에는 해당 상품의 판매대금을 월 판매마감일부터 {c}일 이내에 납품업자등에게 지급하여야 한다.</항내용>
<호><호내용>1. 특약매입거래로 납품받은 상품을 판매하는 경우</호내용></호></항>
<항><항내용>② 대규모유통업자는 직매입거래의 경우에는 해당 상품수령일부터 {d}일 이내에 해당 상품의 대금을 납품업자에게 지급하여야 한다.</항내용></항>
<항><항내용>③ 대규모유통업자가 제1항 및 제2항에서 정한 기한을 초과하여 지급하는 경우에는 그 초과 기간에 대하여 연 100분의 40 이내에서 공정거래위원회가 정하여 고시하는 이율에 따른 이자를 지급하여야 한다.</항내용></항>
</조문단위>
<조문단위><조문번호>8</조문번호><조문가지번호>2</조문가지번호><조문여부>조문</조문여부><조문내용>제8조의2(다른 조문)</조문내용></조문단위>
</조문></법령>"""

NOTICE = """<?xml version="1.0" encoding="UTF-8"?><AdmRulService><행정규칙기본정보>
<시행일자>{eff}</시행일자></행정규칙기본정보><조문내용><![CDATA[Ⅰ. 상품판매대금 등 지연지급 시의 지연이율

「대규모 유통업에서의 거래 공정화에 관한 법률」제8조(상품판매대금등의 지급)제3항의 규정에 의하여 상품판매대금 등을 지연 지급하는 경우 적용되는 지연이율을 연리 {rate}%로 한다.
]]></조문내용><부칙><부칙내용><![CDATA[부칙 <제2021-13호, 2021. 10. 21.>
이 고시는 2021년 10월 21일부터 시행한다.]]></부칙내용></부칙></AdmRulService>"""


def law_search(name: str, mst: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><LawSearch>'
        f"<law><법령일련번호>1</법령일련번호><현행연혁코드>연혁</현행연혁코드><법령명한글>{name}</법령명한글><시행일자>20200101</시행일자></law>"
        f"<law><법령일련번호>{mst}</법령일련번호><현행연혁코드>현행</현행연혁코드><법령명한글>{name}</법령명한글>"
        "<시행일자>20261002</시행일자><공포일자>20260804</공포일자><공포번호>21857</공포번호></law>"
        f"<law><법령일련번호>9</법령일련번호><현행연혁코드>현행</현행연혁코드><법령명한글>{name} 시행령</법령명한글></law>"
        "</LawSearch>"
    )


def adm_search(name: str, sid: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><AdmRulSearch>'
        f"<admrul><행정규칙일련번호>{sid}</행정규칙일련번호><행정규칙명>{name}</행정규칙명>"
        "<현행연혁구분>현행</현행연혁구분><발령일자>20211021</발령일자><발령번호>2021-13</발령번호>"
        "<시행일자>20211021</시행일자></admrul></AdmRulSearch>"
    )


def make_client(d: int = 60, c: int = 40, rate: str = "15.5", reject: bool = False) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlparse(str(req.url)).query).items()}
        if reject:
            return httpx.Response(
                200,
                text="<Response><result>사용자 정보 검증에 실패하였습니다.</result><msg>IP</msg></Response>",
            )
        if req.url.path.endswith("lawSearch.do"):
            if q["target"] == "law":
                return httpx.Response(200, text=law_search(q["query"], "288601"))
            return httpx.Response(200, text=adm_search(q["query"], "2100000205723"))
        if q["target"] == "law":
            assert q["MST"] == "288601" and q["JO"] == "000800"
            return httpx.Response(200, text=ART8.format(c=c, d=d))
        assert q["ID"] == "2100000205723"
        return httpx.Response(200, text=NOTICE.format(eff="20211021", rate=rate))

    return httpx.Client(transport=httpx.MockTransport(handler))


SPECS = drf.SPECS[:2]


@pytest.fixture()
def data_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JETTAE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JETTAE_DRF_OC", "mysecretkey")
    return tmp_path


def test_fetch_parses_article_and_notice_and_redacts_oc(data_tmp: Path) -> None:
    m = drf.fetch_all(client=make_client(), specs=SPECS, log=lambda s: None)
    art = m["documents"]["large_retail_art8"]
    assert art["ok"] and art["serial"] == "288601" and art["effective_date"] == "2026-10-02"
    assert art["paragraphs"][0] == "제8조(상품판매대금 등의 지급)"
    assert any("상품수령일부터 60일" in p for p in art["paragraphs"])
    assert not any("제8조의2" in p for p in art["paragraphs"])  # branch article excluded
    note = m["documents"]["large_retail_interest"]
    assert note["effective_date"] == "2021-10-21" and note["number"] == "2021-13"
    assert any("연리 15.5%" in p for p in note["paragraphs"])
    text = drf.manifest_path().read_text(encoding="utf-8")
    assert "mysecretkey" not in text and "OC=***" in text
    assert (data_tmp / "data" / "raw" / "law" / "large_retail_art8.xml").exists()


def test_check_matches_registry_and_prints_evidence(data_tmp: Path) -> None:
    m = drf.fetch_all(client=make_client(), specs=SPECS, log=lambda s: None)
    items = check.run_checks(m)
    by = {(c.rule, c.param): c for c in items}
    direct = by[(f"{RULE_DIRECT}@2021-10-21", "term_days")]
    assert direct.status == "match" and direct.found_value == "60"
    a, b = direct.span or (0, 0)
    assert (direct.evidence or "")[a:b] == "60"
    assert by[(f"{RULE_INTEREST_RETAIL}@2021-13", "annual_rate(%)")].status == "match"
    assert by[(f"{RULE_INTEREST_RETAIL}@2021-13", "effective_from")].status == "match"
    assert (
        by[(f"{RULE_INTEREST_RETAIL}@2021-13", "annual_rate<=statutory cap(%)")].status == "match"
    )
    amend = by[(f"{RULE_DIRECT}@2026-amendment", "term_days")]
    assert amend.status == "info"
    # subcontract docs were not fetched: reported, but not a failure (optional sources)
    assert by[("kr.subcontract.art13@2009-04-01", "term_days")].status == "source_missing"
    assert check.failed(items) == []


def test_check_detects_mismatch_and_inactive_amendment_in_force(data_tmp: Path) -> None:
    m = drf.fetch_all(client=make_client(d=35, rate="12"), specs=SPECS, log=lambda s: None)
    items = check.run_checks(m)
    st = {(c.rule, c.param): c.status for c in items}
    assert st[(f"{RULE_DIRECT}@2021-10-21", "term_days")] == "mismatch"
    assert st[(f"{RULE_DIRECT}@2026-amendment", "term_days")] == "attention"
    assert st[(f"{RULE_INTEREST_RETAIL}@2021-13", "annual_rate(%)")] == "mismatch"
    assert len(check.failed(items)) >= 3


def test_check_uses_given_registry(data_tmp: Path) -> None:
    m = drf.fetch_all(client=make_client(), specs=SPECS, log=lambda s: None)
    reg = default_registry()
    rv = reg.get(RULE_DIRECT, "2021-10-21")
    reg2 = RuleRegistry(
        [replace(rv, params={**rv.params, "term_days": 59})] + [x for x in reg if x.key != rv.key]
    )
    st = {(c.rule, c.param): c.status for c in check.run_checks(m, reg2)}
    assert st[(f"{RULE_DIRECT}@2021-10-21", "term_days")] == "mismatch"


def test_drf_rejection_is_recorded_not_hidden(data_tmp: Path) -> None:
    m = drf.fetch_all(client=make_client(reject=True), specs=SPECS, log=lambda s: None)
    assert m["failures"] == ["large_retail_art8", "large_retail_interest"]
    assert "사용자 정보 검증에 실패" in m["documents"]["large_retail_art8"]["error"]
    items = check.run_checks(m)
    assert {c.status for c in items if c.source_key.startswith("large_retail")} == {
        "source_missing"
    }
    assert check.failed(items)


def test_redact_url_keeps_public_test_key() -> None:
    assert redact_url("https://x/?OC=test&a=1") == "https://x/?OC=test&a=1"
    assert redact_url("https://x/?a=1&OC=abc") == "https://x/?a=1&OC=***"
