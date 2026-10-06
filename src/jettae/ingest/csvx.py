"""CSV/TSV parsing with encoding detection, delimiter sniffing and per-cell char offsets.

Encoding order (deterministic): UTF-8/UTF-16 BOM → strict UTF-8 → strict CP949 →
charset-normalizer best guess restricted to Korean encodings (EUC-KR is decoded as its
superset CP949). UTF-16 is accepted only with a BOM: short BOM-less CP949 text used to be
guessed as UTF-16-BE and silently turned into unrelated CJK characters. NUL characters are
rejected anywhere in the decoded text (``binary``), the same on SQLite and PostgreSQL.
The decoded text is the document text, so ``char_start``/``char_end`` locate each cell in it.
"""

from __future__ import annotations

import codecs
from collections import Counter
from dataclasses import dataclass

from charset_normalizer import from_bytes

from jettae.domain.status import DocumentStatus
from jettae.ingest.cells import (
    Cell,
    CorruptFile,
    IngestRejected,
    ParsedDoc,
    Row,
    SourceKind,
    Table,
    col_letter,
)
from jettae.ingest.security import DEFAULT_LIMITS, Limits, check_size

DELIMITERS = (",", "\t", ";", "|")
_ALLOWED = ("cp949", "euc_kr", "johab")  # no BOM-less UTF-16 guesses


def decode_text(content: bytes) -> tuple[str, str]:
    """Return ``(text, encoding_name)``. Raises :class:`CorruptFile` if nothing fits."""
    if content.startswith(codecs.BOM_UTF8):
        return content[len(codecs.BOM_UTF8) :].decode("utf-8"), "utf-8-sig"
    if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return content.decode("utf-16"), "utf-16"
        except UnicodeDecodeError as e:
            raise CorruptFile(f"invalid UTF-16 text: {e}", code="bad_encoding") from e
    try:
        return content.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return content.decode("cp949"), "cp949"
    except UnicodeDecodeError:
        pass
    best = from_bytes(content, cp_isolation=list(_ALLOWED)).best()
    if best is not None and best.encoding:
        enc = "cp949" if best.encoding in ("euc_kr", "cp949") else best.encoding
        try:
            return content.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            pass
    raise CorruptFile(
        "text encoding not recognised (tried utf-8, cp949, charset-normalizer)",
        code="bad_encoding",
    )


@dataclass(frozen=True)
class _Field:
    value: str
    start: int  # offset of the value (inside quotes when quoted)
    end: int
    line: int


def tokenize(text: str, delimiter: str, *, max_records: int | None = None) -> list[list[_Field]]:
    """RFC 4180 tokenizer that records the char range of each field in ``text``."""
    records: list[list[_Field]] = []
    fields: list[_Field] = []
    i, n, line = 0, len(text), 1
    while i <= n:
        if max_records is not None and len(records) >= max_records:
            break
        if i == n:
            if fields:
                records.append(fields)
            break
        start_line = line
        if text[i] == '"':
            j = i + 1
            buf: list[str] = []
            while True:
                if j >= n:
                    raise CorruptFile(f"unterminated quoted field at line {start_line}", code="csv")
                ch = text[j]
                if ch == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                if ch == "\n":
                    line += 1
                buf.append(ch)
                j += 1
            f = _Field("".join(buf), i + 1, j, start_line)
            j += 1  # closing quote
            # tolerate garbage between closing quote and delimiter (kept out of the value)
            while j < n and text[j] not in (delimiter, "\n", "\r"):
                j += 1
        else:
            j = i
            while j < n and text[j] not in (delimiter, "\n", "\r"):
                j += 1
            f = _Field(text[i:j], i, j, start_line)
        fields.append(f)
        if j < n and text[j] == delimiter:
            i = j + 1
            if i == n:
                fields.append(_Field("", i, i, line))
            continue
        # end of record
        if j < n and text[j] == "\r":
            j += 1
        if j < n and text[j] == "\n":
            j += 1
        line += 1
        if not (len(fields) == 1 and fields[0].value == "" and fields[0].start == fields[0].end):
            records.append(fields)
        else:
            records.append([])  # blank line keeps record numbering aligned to lines
        fields = []
        i = j
        if i == n:
            break
    return records


def sniff_delimiter(text: str) -> str:
    """Pick the delimiter giving the most consistent multi-column records in the first lines."""
    sample = text[:65536]
    best: tuple[float, int, int, str] | None = None
    for prio, d in enumerate(DELIMITERS):
        try:
            recs = [r for r in tokenize(sample, d, max_records=60) if r]
        except CorruptFile:
            continue
        if len(recs) > 1:
            recs = recs[:-1]  # last sampled record may be truncated
        counts = [len(r) for r in recs]
        if not counts:
            continue
        mode, freq = Counter(counts).most_common(1)[0]
        if mode < 2:
            continue
        consistency = freq / len(counts)
        key = (consistency, mode, -prio, d)
        if best is None or key[:3] > best[:3]:
            best = key
    return best[3] if best else ","


def parse_csv(
    content: bytes,
    *,
    filename: str = "",
    limits: Limits = DEFAULT_LIMITS,
    delimiter: str | None = None,
) -> ParsedDoc:
    check_size(content, limits)
    if b"\x00" in content[:4096] and not content.startswith(
        (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
    ):
        raise CorruptFile("binary data in a text file", code="binary")
    text, enc = decode_text(content)
    if "\x00" in text:
        pos = text.index("\x00")
        raise CorruptFile(f"binary data (NUL) in a text file at character {pos}", code="binary")
    delim = delimiter or sniff_delimiter(text)
    records = tokenize(text, delim)
    if len(records) > limits.max_rows:
        raise IngestRejected(f"{len(records)} rows exceed limit {limits.max_rows}", code="rows")
    rows: list[Row] = []
    for r_idx, rec in enumerate(records, start=1):
        if len(rec) > limits.max_cols:
            raise IngestRejected(f"row {r_idx}: too many columns", code="cols")
        cells = tuple(
            Cell(
                f.value,
                {
                    "row": r_idx,
                    "col": c_idx,
                    "col_letter": col_letter(c_idx),
                    "line": f.line,
                    "char_start": f.start,
                    "char_end": f.end,
                },
            )
            for c_idx, f in enumerate(rec, start=1)
        )
        rows.append(Row(r_idx, cells))
    doc = ParsedDoc(
        source_kind=SourceKind.CSV,
        status=DocumentStatus.PARSED,
        tables=[Table("csv", tuple(rows), {"delimiter": delim})],
        text=text,
        filename=filename,
        encoding=enc,
        delimiter=delim,
    )
    if not any(not r.is_empty for r in rows):
        doc.warnings.append("no data rows")
    return doc
