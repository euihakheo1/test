from jettae.ingest.cells import Cell, Row, Table
from jettae.ingest.formats import BANK, SETTLEMENT, recognize_table, score_format
from jettae.ingest.mapping import (
    Confidence,
    FieldKind,
    FieldSpec,
    confirm_mapping,
    header_score,
    normalize_header,
    suggest_mapping,
)
from jettae.ingest.table import detect_header, is_total_row


def _table(rows: list[list[str]]) -> Table:
    return Table(
        "t",
        tuple(
            Row(i, tuple(Cell(v, {"row": i, "col": j + 1}) for j, v in enumerate(r)))
            for i, r in enumerate(rows, start=1)
        ),
    )


DATE = FieldSpec("d", "일자", FieldKind.DATE, ("거래일자", "거래일시"), True)
AMT = FieldSpec("a", "입금액", FieldKind.AMOUNT, ("입금액",), True, ("입금자",))


def test_normalize_header():
    assert normalize_header(" 입금액(원) ") == "입금액"
    assert normalize_header("영수/청구 구분") == "영수청구구분"
    assert normalize_header("보낸분/받는분") == "보낸분받는분"


def test_header_score_buckets_are_deterministic():
    assert header_score("거래일자", DATE)[1] is Confidence.HIGH
    assert header_score("거래일자(KST)", DATE)[1] is Confidence.MEDIUM
    assert header_score("일자", DATE)[1] is Confidence.LOW
    assert header_score("입금자명", AMT)[0] == 0  # excluded


def test_sample_values_downgrade_bucket():
    s = suggest_mapping(
        "x", ["거래일자", "입금액"], [["가나다", "1,000"], ["라마", "2,000"]], [DATE, AMT]
    )
    m = {x.field: x for x in s.matches}
    assert m["d"].confidence is Confidence.MEDIUM  # header exact, values are not dates
    assert m["a"].confidence is Confidence.HIGH
    assert s.needs_confirmation and s.overall is Confidence.MEDIUM
    ok = suggest_mapping("x", ["거래일자", "입금액"], [["2025-01-02", "1,000"]], [DATE, AMT])
    assert not ok.needs_confirmation and ok.overall is Confidence.HIGH


class FakeLlm:
    name = "fake-llm"

    def __init__(self, proposal):
        self.proposal = proposal
        self.seen = None

    def suggest(self, headers, samples, fields):
        self.seen = [f.name for f in fields]
        return self.proposal


def test_llm_suggester_only_fills_gaps_as_low_and_needs_confirmation():
    llm = FakeLlm({"a": 1, "d": 0})
    s = suggest_mapping(
        "x", ["거래일자", "들어온돈"], [["2025-01-02", "1,000"]], [DATE, AMT], suggester=llm
    )
    assert llm.seen == ["a"]  # only unmapped fields are asked
    m = {x.field: x for x in s.matches}
    assert m["d"].confidence is Confidence.HIGH  # rule result not overridden
    assert m["a"].confidence is Confidence.LOW and "fake-llm" in m["a"].reason
    assert s.needs_confirmation
    # a proposal whose sample values do not fit is dropped
    bad = suggest_mapping(
        "x",
        ["거래일자", "메모"],
        [["2025-01-02", "가나"]],
        [DATE, AMT],
        suggester=FakeLlm({"a": 1}),
    )
    assert bad.unmapped_required == ("a",)

    class Broken:
        name = "broken"

        def suggest(self, headers, samples, fields):
            raise TimeoutError("slow")

    s3 = suggest_mapping("x", ["거래일자", "x"], [], [DATE, AMT], suggester=Broken())
    assert any("suggester_error" in src for src in s3.sources)


def test_confirm_mapping_marks_confirmed_and_moves_columns():
    s = suggest_mapping("x", ["거래일자", "들어온돈"], [], [DATE, AMT])
    assert s.unmapped_required == ("a",)
    c = confirm_mapping(s, ["거래일자", "들어온돈"], {"a": "들어온돈"}, [DATE, AMT])
    assert c.match("a").confidence is Confidence.CONFIRMED
    assert not c.needs_confirmation
    c2 = confirm_mapping(c, ["거래일자", "들어온돈"], {"d": 1}, [DATE, AMT])
    assert c2.column_of("d") == 1 and c2.column_of("a") is None  # column reassigned


def test_header_detection_skips_title_and_finds_totals_and_repeats():
    t = _table(
        [
            ["거래내역 조회 결과", "", ""],
            ["", "", ""],
            ["거래일자", "적요", "입금액"],
            ["2025-01-02", "이체", "1,000"],
            ["거래일자", "적요", "입금액"],  # page-break repeated header
            ["2025-01-03", "이체", "2,000"],
            ["소계", "", "3,000"],
        ]
    )
    from jettae.ingest.formats import vocabulary

    v = detect_header(t, vocabulary())
    assert v.header_rows == (3,)
    assert v.preamble == "거래내역 조회 결과"
    assert [r.index for r in v.data] == [4, 6]
    assert [r.index for r in v.totals] == [7]
    assert v.skipped == ((5, "repeated header"),)
    assert is_total_row(t.rows[6])
    assert not is_total_row(t.rows[3])


def test_format_recognition_prefers_matching_format():
    bank = _table(
        [
            ["거래일시", "적요", "찾으신금액", "맡기신금액", "잔액"],
            ["2025.01.02 10:00", "이체", "0", "1,000", "1,000"],
        ]
    )
    rec = recognize_table(bank)
    assert rec.recognized and rec.best.spec is BANK
    settle = _table([["정산번호", "판매마감일", "정산금액"], ["1", "2025-01-31", "1,000"]])
    rec2 = recognize_table(settle)
    assert rec2.recognized and rec2.best.spec is SETTLEMENT
    view = detect_header(settle, ["정산번호"])
    assert score_format(SETTLEMENT, view).complete
