from datetime import date

import pytest
from ing_helpers import build_pdf, grid_table, scanned_pdf, text_op

from jettae.domain.money import Money
from jettae.domain.status import DocumentStatus, TradeType
from jettae.ingest import (
    IngestOptions,
    OcrCell,
    OcrFailed,
    OcrPage,
    OcrProvider,
    ingest_bytes,
    parse_bytes,
    safe_parse,
)
from jettae.ingest.pdfx import parse_pdf
from jettae.verify.checks import check_citation

ROWS = [
    ["정산번호", "거래형태", "판매마감일", "정산금액"],
    ["S-001", "특약매입", "2025-09-30", "3,000,000"],
    ["S-002", "직매입", "", "1,500,000"],
    ["합계", "", "", "4,500,000"],
]


def settlement_pdf() -> bytes:
    page1 = text_op(50, 780, "가나유통 정산서 2025년 9월", 12) + grid_table(
        50, 740, [90, 90, 110, 110], ROWS
    )
    return build_pdf([page1])


def test_text_pdf_table_cells_have_page_bbox_and_char_offsets():
    doc = parse_pdf(settlement_pdf(), filename="s.pdf")
    assert doc.status is DocumentStatus.PARSED
    assert doc.meta["pages"][0]["kind"] == "text"
    (t,) = doc.tables
    assert [r.texts() for r in t.rows] == ROWS
    page_text = doc.text  # single page: document text == page text
    for r in t.rows:
        for c in r.cells:
            if not c.text:
                continue
            loc = c.locator
            assert loc["page"] == 1 and len(loc["bbox"]) == 4
            assert all(isinstance(v, int) for v in loc["bbox"])
            assert page_text[loc["char_start"] : loc["char_end"]] == c.text


def test_text_pdf_settlement_records_and_spans():
    res = ingest_bytes(
        settlement_pdf(),
        "s.pdf",
        options=IngestOptions(counterparty_override="가나유통"),
        doc_version_id="d",
    )
    assert res.status is DocumentStatus.PARSED
    a, b = res.records
    assert a.settlement_ref == "S-001" and a.trade_type is TradeType.CONSIGNMENT
    assert a.sales_close_date == date(2025, 9, 30) and a.amount == Money(3_000_000)
    assert b.trade_type is TradeType.DIRECT and b.goods_received_date is None
    assert "goods_received_date" in b.missing  # 직매입 needs 상품수령일: not in this sheet
    (tc,) = res.totals
    assert tc.stated == 4_500_000 and tc.matches
    for f in res.facts:
        if f.span is not None:
            assert f.span.locator["page"] == 1
            assert check_citation(f.span, res.plan.parsed.text).passed


def test_scanned_pdf_without_ocr_is_unsupported_scan_not_empty():
    raw = scanned_pdf(2)
    doc = safe_parse(raw, "scan.pdf")
    assert doc.status is DocumentStatus.UNSUPPORTED_SCAN
    assert [p["kind"] for p in doc.meta["pages"]] == ["image_only", "image_only"]
    res = ingest_bytes(raw, "scan.pdf")
    assert res.status is DocumentStatus.UNSUPPORTED_SCAN and res.records == []


class FakeOcr:
    name = "fake-ocr"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[int]] = []

    def ocr_pdf_pages(self, content, pages):
        self.calls.append(list(pages))
        if self.fail:
            raise RuntimeError("timeout")
        tbl = [[OcrCell(h) for h in ROWS[0]], [OcrCell(v) for v in ROWS[1]]]
        return [
            OcrPage(
                page=p,
                text="\n".join(" ".join(r) for r in ROWS[:2]),
                tables=[tbl],
                provider=self.name,
            )
            for p in pages[:1]
        ]


def test_scanned_pdf_with_ocr_provider_hook():
    ocr = FakeOcr()
    assert isinstance(ocr, OcrProvider)
    doc = parse_bytes(scanned_pdf(1), "scan.pdf", ocr=ocr)
    assert ocr.calls == [[1]]
    assert doc.status is DocumentStatus.PARSED
    assert doc.tables[0].rows[1].cells[0].locator["ocr"] == "fake-ocr"
    assert any("OCR" in w for w in doc.warnings)
    res = ingest_bytes(
        scanned_pdf(1), "scan.pdf", ocr=ocr, options=IngestOptions(counterparty_override="가나")
    )
    assert len(res.records) == 1


def test_ocr_failure_is_failed_not_no_rows():
    with pytest.raises(OcrFailed):
        parse_bytes(scanned_pdf(1), "scan.pdf", ocr=FakeOcr(fail=True))
    doc = safe_parse(scanned_pdf(1), "scan.pdf", ocr=FakeOcr(fail=True))
    assert doc.status is DocumentStatus.FAILED and "ocr_failed" in (doc.reason or "")


def test_mixed_pdf_reports_unread_pages():
    text_page = text_op(50, 780, "가나유통 정산서") + grid_table(50, 740, [90, 90, 110, 110], ROWS)
    img_pdf = scanned_pdf(1)
    # build a 2-page PDF: text page + a page with an inline image XObject
    img_page = b"q 200 0 0 100 50 600 cm BI /W 2 /H 2 /CS /G /BPC 8 ID \x00\xff\xff\x00 EI Q\n"
    raw = build_pdf([text_page, img_page])
    doc = parse_pdf(raw)
    assert [p["kind"] for p in doc.meta["pages"]] == ["text", "image_only"]
    assert doc.status is DocumentStatus.PARSED and doc.meta["unread_pages"] == [2]
    assert doc.reason == "partial_scan"
    assert img_pdf  # pillow fixture still valid


def test_corrupt_and_blank_pdf():
    doc = safe_parse(b"%PDF-1.4\n garbage without objects", "bad.pdf")
    assert doc.status is DocumentStatus.CORRUPT
    blank = parse_pdf(build_pdf([b""]))
    assert blank.status is DocumentStatus.PARSED
    assert blank.meta["pages"][0]["kind"] == "blank"
    assert any("no text and no images" in w for w in blank.warnings)
