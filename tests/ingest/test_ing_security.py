import io
import zipfile

import pytest

from jettae.domain.status import DocumentStatus
from jettae.ingest import IngestRejected, Limits, ingest_bytes, parse_bytes, safe_parse, sniff_kind
from jettae.ingest.cells import SourceKind
from jettae.ingest.detect import OLE_MAGIC
from jettae.ingest.security import check_ole_macros


def test_size_and_row_caps():
    with pytest.raises(IngestRejected) as e:
        parse_bytes(b"a,b\n" * 100, "x.csv", limits=Limits(max_bytes=50))
    assert e.value.code == "too_large"
    with pytest.raises(IngestRejected) as e2:
        parse_bytes(b"a,b\n" * 100, "x.csv", limits=Limits(max_rows=10))
    assert e2.value.code == "rows"
    doc = safe_parse(b"", "empty.csv")
    assert doc.status is DocumentStatus.FAILED and "empty_file" in (doc.reason or "")


def test_zip_bomb_ratio_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<workbook/>")
        z.writestr("xl/worksheets/sheet1.xml", "0" * 5_000_000)
    with pytest.raises(IngestRejected) as e:
        parse_bytes(buf.getvalue(), "bomb.xlsx")
    assert e.value.code == "zip_bomb"


def test_legacy_xls_is_reported_not_parsed_and_macros_rejected():
    fake = OLE_MAGIC + b"\x00" * 600
    assert sniff_kind(fake) is SourceKind.XLS_BIFF
    doc = safe_parse(fake, "old.xls")
    assert doc.status is DocumentStatus.FAILED and ".xls" in (doc.reason or "")
    with pytest.raises(IngestRejected):
        check_ole_macros(fake + "_VBA_PROJECT".encode("utf-16-le"))


def test_unknown_binary_is_failed():
    doc = safe_parse(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "x.png")
    assert doc.status is DocumentStatus.FAILED and doc.source_kind is SourceKind.UNKNOWN


def test_html_disguised_as_xls_bank_export():
    html = """<html><head><meta charset="euc-kr"></head><body>
    <table><tr><td colspan="4">하나은행 거래내역 계좌번호: 123-456789-01234</td></tr>
    <tr><th>거래일시</th><th>적요</th><th>출금액</th><th>입금액</th><th>잔액</th></tr>
    <tr><td>2025-10-20 10:00</td><td>(주)가나유통</td><td>0</td>
    <td>2,000,000</td><td>2,000,000</td></tr>
    <script>alert(1)</script>
    </table></body></html>"""
    raw = html.encode("cp949")
    assert sniff_kind(raw) is SourceKind.HTML
    res = ingest_bytes(raw, "hana.xls")
    assert res.plan.parsed.encoding == "cp949"
    assert res.status is DocumentStatus.PARSED
    (t,) = res.records
    assert t.amount.amount == 2_000_000 and t.account == "123-456789-01234"
    merged = res.plan.parsed.tables[0].rows[0].cells[1]
    assert merged.locator["merged"] == "A1:D1"
