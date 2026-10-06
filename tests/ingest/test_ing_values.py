from datetime import date, time
from decimal import Decimal

import pytest

from jettae.ingest.cells import Cell
from jettae.ingest.security import escape_formula, safe_filename, strip_formula_prefix
from jettae.ingest.values import (
    ValueParseError,
    excel_serial_to_date,
    parse_amount,
    parse_brn,
    parse_date,
    parse_id,
    parse_time,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,234,000", 1234000),
        ("1,234,000원", 1234000),
        ("₩ 50,000", 50000),
        ("-3,000", -3000),
        ("(3,000)", -3000),
        ("△3,000", -3000),
        ("▲ 3,000", -3000),
        ("3,000-", -3000),
        ("+500", 500),
        ("0", 0),
        ("1000.00", 1000),
        ("", None),
        ("-", None),
    ],
)
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("bad", ["12.5", "1,2,3", "abc", "1.000,00", "=1+1"])
def test_parse_amount_rejects(bad):
    with pytest.raises(ValueParseError):
        parse_amount(bad)


def test_amount_from_typed_cells_never_float():
    assert parse_amount(Cell("1000", {}, 1000)) == 1000
    assert parse_amount(Cell("1000", {}, Decimal("1000"))) == 1000
    with pytest.raises(ValueParseError):
        parse_amount(Cell("0.5", {}, Decimal("0.5")))


@pytest.mark.parametrize(
    ("text", "d", "t"),
    [
        ("2025-08-07", date(2025, 8, 7), None),
        ("2025.08.07", date(2025, 8, 7), None),
        ("2025. 8. 7.", date(2025, 8, 7), None),
        ("2025/08/07 13:45", date(2025, 8, 7), time(13, 45)),
        ("2025.08.07 13:45:09", date(2025, 8, 7), time(13, 45, 9)),
        ("20250807", date(2025, 8, 7), None),
        ("20250807134509", date(2025, 8, 7), time(13, 45, 9)),
        ("2025년 8월 7일", date(2025, 8, 7), None),
    ],
)
def test_parse_date(text, d, t):
    v = parse_date(text)
    assert v is not None and v.value == d and v.time == t


def test_excel_serial_dates():
    # 45876 = 2025-08-07 in the 1900 system; 1904 system is 1462 days later
    assert excel_serial_to_date(45876) == date(2025, 8, 7)
    assert excel_serial_to_date(45876 - 1462, epoch_1904=True) == date(2025, 8, 7)
    v = parse_date("45876")
    assert v is not None and v.value == date(2025, 8, 7) and v.note == "excel_serial"
    with pytest.raises(ValueParseError):
        parse_date("12345")  # implausible serial (1933)
    with pytest.raises(ValueParseError):
        parse_date("45876", allow_serial=False)


@pytest.mark.parametrize("bad", ["2025-02-30", "07/08/2025", "어제"])
def test_parse_date_rejects(bad):
    with pytest.raises(ValueParseError):
        parse_date(bad)


def test_ids_keep_leading_zeros_and_brn_format():
    assert parse_id(" 000123 ") == "000123"
    assert parse_brn("1234567890") == "123-45-67890"
    assert parse_brn("123-45-67890") == "123-45-67890"
    assert parse_time("0930") == time(9, 30)


def test_escape_formula():
    assert escape_formula('=HYPERLINK("x")') == '\'=HYPERLINK("x")'
    assert escape_formula("@SUM(A1)") == "'@SUM(A1)"
    assert escape_formula("+cmd") == "'+cmd"
    assert escape_formula("\uff1d1+1") == "'\uff1d1+1"  # full-width equals
    assert escape_formula("-1,000") == "-1,000"  # plain number kept
    assert escape_formula("-2+3") == "'-2+3"
    assert escape_formula("가나유통") == "가나유통"
    assert strip_formula_prefix("=@SUM(A1)") == "SUM(A1)"
    assert strip_formula_prefix("-1000") == "-1000"


def test_safe_filename():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\x\\..\\정산서.xlsx") == "정산서.xlsx"
    assert safe_filename("") == "upload"
