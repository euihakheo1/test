"""Property: our offset-tracking tokenizer reads what the stdlib csv writer wrote."""

import csv
import io

from hypothesis import example, given, settings
from hypothesis import strategies as st

from jettae.ingest.csvx import parse_csv

cell = st.text(
    alphabet=st.sampled_from(list('ab가나 ,"\n0-=')),
    min_size=0,
    max_size=8,
)


@settings(max_examples=150, deadline=None)
@given(
    st.lists(st.lists(cell, min_size=2, max_size=4), min_size=1, max_size=6).filter(
        lambda rows: all(any(c.strip() for c in r) for r in rows)
    ),
    st.sampled_from(["utf-8", "cp949", "utf-8-sig"]),
)
# short BOM-less cp949 inputs that charset-normalizer used to guess as UTF-16-BE
@example(rows=[["", "", "00가"]], enc="cp949")
@example(rows=[["00=0", "가,"]], enc="cp949")
@example(rows=[["", "0가"]], enc="cp949")
def test_roundtrip_values_and_offsets(rows, enc):
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerows(rows)
    doc = parse_csv(buf.getvalue().encode(enc), delimiter=",")
    got = [r.texts() for r in doc.tables[0].rows]
    assert got == rows
    for r in doc.tables[0].rows:
        for c in r.cells:
            s, e = c.locator["char_start"], c.locator["char_end"]
            if '"' not in c.text:
                assert doc.text[s:e] == c.text
            else:
                assert doc.text[s:e].replace('""', '"') == c.text
