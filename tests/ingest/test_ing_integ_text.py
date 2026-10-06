"""Integrator regressions: BOM-less cp949 detection and NUL handling (hand-written bytes)."""

import pytest

from jettae.db.repos import db_safe_text
from jettae.ingest.cells import CorruptFile
from jettae.ingest.csvx import decode_text, parse_csv


@pytest.mark.parametrize("text", ["가,1\r\n", "금액,0가\r\n", '00=0,"가,"\r\n'])
def test_short_cp949_is_not_guessed_as_utf16(text):
    decoded, enc = decode_text(text.encode("cp949"))
    assert enc == "cp949"
    assert decoded == text


def test_utf16_with_bom_still_decodes():
    decoded, enc = decode_text("금액,1\r\n".encode("utf-16"))
    assert enc == "utf-16" and decoded == "금액,1\r\n"


def test_nul_after_first_4k_is_rejected_as_binary():
    rows = ["일자,금액"] + [f"2025-09-{(i % 28) + 1:02d},{1000 + i}" for i in range(800)]
    text = "\r\n".join(rows)
    pos = 9000
    assert len(text) > pos
    content = (text[:pos] + "\x00" + text[pos:]).encode("utf-8")
    with pytest.raises(CorruptFile) as e:
        parse_csv(content)
    assert e.value.code == "binary"


def test_db_safe_text_keeps_offsets():
    assert db_safe_text(None) is None
    assert db_safe_text("abc") == "abc"
    out = db_safe_text("a\x00b")
    assert out == "a\ufffdb" and len(out) == 3
