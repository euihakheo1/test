from datetime import date

from ing_helpers import TENANT

from jettae.domain.models import BankTxn
from jettae.domain.money import Money
from jettae.domain.status import DocumentStatus
from jettae.ingest import IngestOptions, analyze, build_records, ingest_bytes, safe_parse
from jettae.ingest.csvx import decode_text, parse_csv, sniff_delimiter, tokenize
from jettae.verify.checks import check_citation

KB_CSV = (
    "국민은행 거래내역조회\r\n"
    "계좌번호 : 123456-01-234567\r\n"
    "\r\n"
    "거래일시,적요,기재내용,찾으신금액(원),맡기신금액(원),잔액(원),거래점\r\n"
    '2025.10.20 10:15:00,타행이체,(주)가나유통,0,"9,900,000","12,000,000",본점\r\n'
    '2025.10.21 09:00:00,인터넷,"사무실 임대료, 10월","1,000,000",0,"11,000,000",강남\r\n'
    '2025.10.22 14:30:00,타행이체취소,(주)가나유통,"9,900,000",0,"1,100,000",본점\r\n'
    '합계,,,"10,900,000","9,900,000",,\r\n'
)


def test_cp949_detected_and_offsets_point_into_text():
    raw = KB_CSV.encode("cp949")
    doc = parse_csv(raw, filename="kb.csv")
    assert doc.encoding == "cp949"
    assert doc.delimiter == ","
    t = doc.tables[0]
    for row in t.rows:
        for c in row.cells:
            s, e = c.locator["char_start"], c.locator["char_end"]
            assert doc.text[s:e] == c.text  # quotes excluded, value exact
    # quoted value with a comma stays one cell
    row6 = next(r for r in t.rows if r.index == 6)
    assert row6.cells[2].text == "사무실 임대료, 10월"
    assert row6.cells[2].locator["line"] == 6


def test_kb_bank_csv_to_bank_txns():
    res = ingest_bytes(
        KB_CSV.encode("cp949"), "kb_거래내역.csv", tenant_id=TENANT, doc_version_id="dv1"
    )
    assert res.status is DocumentStatus.PARSED
    tp = res.plan.tables[0]
    assert tp.spec is not None and tp.spec.id == "kr_bank_txn"
    view = tp.recognition.view
    assert view.header_rows == (4,)
    assert [r.index for r in view.totals] == [8]  # 합계 row separated, not a transaction
    txns = res.records
    assert all(isinstance(t, BankTxn) for t in txns)
    assert [t.amount for t in txns] == [Money(9_900_000), Money(-1_000_000), Money(-9_900_000)]
    assert txns[0].booked_date == date(2025, 10, 20)
    assert txns[0].account == "123456-01-234567"  # from the preamble line
    assert txns[0].counterparty is None and "counterparty" in txns[0].missing  # no explicit column
    assert "(주)가나유통" in txns[0].memo
    # reversal marker recorded as a fact, amount sign untouched
    rev = [f for f in res.facts if f.kind == "bank_txn.reversal_marker"]
    assert len(rev) == 1 and rev[0].subject_id == txns[2].id
    # 합계 row check: stated totals vs computed
    by_field = {c.field: c for c in res.totals}
    assert by_field["withdrawal"].stated == 10_900_000 and by_field["withdrawal"].matches
    assert by_field["deposit"].stated == 9_900_000 and by_field["deposit"].matches


def test_every_fact_has_span_found_in_document_text():
    raw = KB_CSV.encode("cp949")
    res = ingest_bytes(raw, "kb.csv", tenant_id=TENANT, doc_version_id="dv1")
    text = res.plan.parsed.text
    spans = [f for f in res.facts if f.span is not None]
    assert spans
    for f in spans:
        assert f.span.doc_version_id == "dv1"
        assert check_citation(f.span, text).passed, f
        loc = f.span.locator
        assert text[loc["char_start"] : loc["char_end"]] == f.span.excerpt


def test_utf8_sig_tab_delimited_and_leading_zero_ids():
    body = "거래일자\t거래시간\t적요\t출금액\t입금액\t잔액\t내용\t거래점\n"
    body += "2025-10-20\t09:01:02\t입금\t0\t500000\t500000\t000123 주문\t서울\n"
    raw = "﻿".encode() + body.encode("utf-8")
    doc = parse_csv(raw)
    assert doc.encoding == "utf-8-sig" and doc.delimiter == "\t"
    res = ingest_bytes(raw, "shinhan.csv")
    assert res.status is DocumentStatus.PARSED
    (t,) = res.records
    assert t.amount == Money(500_000)
    assert "000123" in t.memo
    times = [f.value for f in res.facts if f.kind == "bank_txn.txn_time"]
    assert times == ["09:01:02"]


def test_formula_like_text_is_kept_as_text_not_evaluated():
    raw = '거래일자,적요,입금액,출금액,잔액\n2025-10-20,=HYPERLINK("http://x"),1000,0,1000\n'
    res = ingest_bytes(raw.encode("utf-8"), "x.csv")
    (t,) = res.records
    assert t.memo.startswith("=HYPERLINK")


def test_tokenizer_handles_quotes_newlines_and_blank_lines():
    text = 'a,"b ""q"" c","multi\nline"\r\n\r\nx,y,\n'
    recs = tokenize(text, ",")
    assert [f.value for f in recs[0]] == ["a", 'b "q" c', "multi\nline"]
    assert recs[1] == []  # blank line keeps numbering
    assert [f.value for f in recs[2]] == ["x", "y", ""]
    assert sniff_delimiter("a;b;c\n1;2;3\n4;5;6\n") == ";"
    assert sniff_delimiter("a|b\n1|2\n") == "|"


def test_euc_kr_bytes_decode_as_cp949():
    raw = "똠방각하,금액\n".encode("cp949")  # 똠 is CP949-only (not in EUC-KR)
    text, enc = decode_text(raw)
    assert text.startswith("똠방각하") and enc == "cp949"


def test_unknown_csv_layout_needs_mapping_then_user_mapping_parses():
    raw = "날,돈,메모\n2025-10-20,1000,가나\n".encode()
    plan = analyze(safe_parse(raw, "x.csv"))
    assert plan.status is DocumentStatus.NEEDS_MAPPING
    opts = IngestOptions(
        format_id="kr_bank_txn",
        mapping={"*": {"txn_date": "날", "deposit": 1, "description": "메모"}},
    )
    plan2 = analyze(safe_parse(raw, "x.csv"), opts)
    assert plan2.status is DocumentStatus.PARSED
    res = build_records(plan2, tenant_id=TENANT, doc_version_id="dv", options=opts)
    (t,) = res.records
    assert t.amount == Money(1000) and t.booked_date == date(2025, 10, 20)


def test_both_deposit_and_withdrawal_on_one_row_is_an_issue_not_a_guess():
    raw = "거래일자,입금액,출금액\n2025-10-20,1000,500\n2025-10-21,,\n".encode()
    res = ingest_bytes(raw, "x.csv")
    assert res.records == []
    msgs = [i.message for i in res.issues]
    assert any("함께" in m for m in msgs) and any("없음" in m for m in msgs)
