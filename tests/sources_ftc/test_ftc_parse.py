"""Parser tests on real DRF decision XML (see fixtures/prov.json)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from ftc_test_helpers import FIXTURES, decision, fixture_bytes

from jettae.sources.ftc.parse import (
    delay_table_score,
    parse_decision,
    parse_kdate,
    unit_multiplier,
)


def test_fixture_provenance_is_recorded() -> None:
    prov = json.loads((FIXTURES / "prov.json").read_text(encoding="utf-8"))
    files = {p["file"] for p in prov}
    assert files == {"f16947.xml", "x19065.xml", "x19005.xml", "x19299.xml"}
    for p in prov:
        assert p["source_url"].startswith("https://www.law.go.kr/DRF/lawService.do?OC={OC}")
        assert len(p["source_sha256"]) == 64


def test_metadata_16947() -> None:
    d = decision("f16947.xml")
    assert d.decision_id == "16947"
    assert d.case_no == "2021유통1898"
    assert d.decision_date == date(2023, 9, 8)
    assert "이마트" in d.title
    assert {"주문", "이유", "결정요지"} <= set(d.sections)


def test_footnote_list_and_markers_16947() -> None:
    d = decision("f16947.xml")
    listed = [f for f in d.footnotes if not f.inline]
    assert [f.no for f in listed] == list(range(1, 22))
    fn8 = next(f for f in listed if f.no == 8)
    assert "마지막날이 공휴일인 경우 그 익일" in fn8.text
    assert fn8.section == "각주:8"
    # the <각주>8</각주> marker sits next to the 표 5 image in 이유
    assert fn8.marker_section == "이유"
    assert fn8.marker_pos is not None
    mark = "<각주>8</각주>"
    assert d.text("이유")[fn8.marker_pos : fn8.marker_pos + len(mark)] == mark


def test_tables_and_captions_16947() -> None:
    d = decision("f16947.xml")
    by_seq = {t.flseq: t for t in d.tables}
    t5, t6 = by_seq["133422117"], by_seq["133422119"]
    assert (t5.label, t5.caption) == ("표 5", "상품판매대금 지연지급(공탁) 내역")
    assert (t6.label, t6.caption) == ("표 6", "지연이자 미지급 내역")
    assert t5.unit_note == "단위: 원, VAT 포함"
    assert delay_table_score(t5)[0] == 2 and delay_table_score(t6)[0] == 2
    # market / dispatch tables are not delay tables
    assert delay_table_score(by_seq["133422115"])[0] == 0
    assert delay_table_score(by_seq["133422121"])[0] == 0
    # offsets reproduce the img tag
    txt = d.text(t5.section)
    assert txt[t5.char_start : t5.char_end].startswith(
        '<img src="/LSW/flDownload.do?flSeq=133422117"'
    )


def test_caption_before_and_after_image_19065() -> None:
    d = decision("x19065.xml")
    by_seq = {t.flseq: t for t in d.tables}
    # caption + unit line above the image
    assert by_seq["163492277"].label == "표 11"
    assert by_seq["163492277"].unit_note == "단위: 원, VAT 포함"
    # caption directly above, no unit line
    assert by_seq["163492279"].label == "표 12"
    assert by_seq["163492279"].unit_note is None
    # caption printed *below* the image
    assert by_seq["163492281"].label == "표 13"
    assert by_seq["163492275"].label == "표 10"
    assert delay_table_score(by_seq["163492275"])[0] == 0  # 내부 메일
    assert all(delay_table_score(by_seq[s])[0] == 2 for s in ("163492277", "163492283"))


def test_inline_footnotes_19065() -> None:
    d = decision("x19065.xml")
    inline = [f for f in d.footnotes if f.inline]
    texts = [f.text for f in inline]
    assert any(t.startswith("직매입거래의 경우 매입기간 종료일을") for t in texts)
    assert any("상품수령일부터 60일 이내" in t for t in texts)
    # a footnote that mentions "<표 11>" is still recognised
    rounding = next(f for f in inline if "원 단위 미만 절사" in f.text)
    assert "<표 11>" in rounding.text
    assert rounding.confidence == "low"
    for f in inline:
        assert d.text(f.section)[f.char_start : f.char_end].strip() == f.text


def test_bracket_unit_line_and_caption_19005() -> None:
    d = decision("x19005.xml")
    t = next(t for t in d.tables if t.flseq == "162805909")
    assert t.label == "표 56"
    assert t.caption == "상품대금 지연지급 및 지연이자 미지급 현황"
    assert t.unit_note == "단위: 개, 건, 일, 원(부가가치세 포함)"
    assert delay_table_score(t)[0] == 2


def test_unlinked_and_continuation_images_19299() -> None:
    d = decision("x19299.xml")
    t11 = next(t for t in d.tables if t.flseq == "167807599")
    assert t11.unit_note == "단위: 천 원, 부가가치세 포함"
    assert unit_multiplier(t11.unit_note) == 1000
    byeolji = [t for t in d.tables if t.section == "별지"]
    unlinked = [t for t in byeolji if t.flseq is None]
    assert unlinked and all(t.src.startswith("table_image_") for t in unlinked)
    assert all(t.continuation for t in unlinked)
    assert {t.label for t in byeolji} == {"별지 1", "별지 2"}
    # 계약서면 지연발급 is not a payment-delay table
    assert all(delay_table_score(t)[0] == 0 for t in byeolji if t.label == "별지 1")


@pytest.mark.parametrize(
    ("note", "mult"),
    [("단위: 원, VAT 포함", 1), ("단위: 천 원", 1000), ("단위: 백만 원", 1_000_000), (None, 1)],
)
def test_unit_multiplier(note: str | None, mult: int) -> None:
    assert unit_multiplier(note) == mult


def test_parse_kdate() -> None:
    assert parse_kdate("2026.2.10.") == date(2026, 2, 10)
    assert parse_kdate("2026. 1. 29.") == date(2026, 1, 29)
    assert parse_kdate("") is None
    assert parse_kdate("2026.2.30.") is None


def test_rejects_non_ftc_xml() -> None:
    err = (
        "<?xml version='1.0' encoding='UTF-8'?><Response><result>사용자 정보 검증에 실패"
        "</result></Response>"
    )
    with pytest.raises(ValueError):
        parse_decision(err)
    assert parse_decision(fixture_bytes("f16947.xml")).decision_id == "16947"
