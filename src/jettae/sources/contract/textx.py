"""Document -> paragraphs for contract files: HWP 5.0 (.hwp), HWPX (.hwpx, zip+xml), PDF."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

from lxml import etree

from jettae.sources.contract.cfb import MAGIC
from jettae.sources.contract.hwp5 import HwpError, Paragraph, read_hwp


class UnsupportedDocument(ValueError):
    pass


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def read_hwpx(data: bytes) -> list[Paragraph]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise UnsupportedDocument(f"not a zip/HWPX file: {e}") from e
    secs = sorted(
        (n for n in z.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n, re.I)),
        key=lambda n: int(re.findall(r"\d+", n.rsplit("/", 1)[1])[0]),
    )
    if not secs:
        raise UnsupportedDocument("HWPX without Contents/section*.xml")
    out: list[Paragraph] = []
    for s_i, name in enumerate(secs):
        root = etree.fromstring(z.read(name))
        idx = -1
        for p in root.iter():
            if _local(p.tag) != "p":
                continue
            idx += 1
            parts: list[str] = []
            for t in p.iter():
                if _local(t.tag) != "t":
                    continue
                anc = t.getparent()
                while anc is not None and _local(anc.tag) != "p":
                    anc = anc.getparent()
                if anc is p:
                    parts.append("".join(t.itertext()))
            level = sum(1 for a in p.iterancestors() if _local(a.tag) == "p")
            text = "".join(parts)
            if text.strip():
                out.append(Paragraph(s_i, idx, level, text))
    return out


def read_pdf(data: bytes) -> list[Paragraph]:
    import pdfplumber

    out: list[Paragraph] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for i, line in enumerate(text.splitlines()):
                if line.strip():
                    out.append(Paragraph(pno, i, 0, line))
    if not out:
        raise UnsupportedDocument("PDF has no text layer (scanned?)")
    return out


def detect(data: bytes, name: str = "") -> str:
    if data[:8] == MAGIC:
        return "hwp"
    if data[:4] == b"PK\x03\x04":
        return "hwpx"
    if data[:5] == b"%PDF-":
        return "pdf"
    ext = Path(name).suffix.lower().lstrip(".")
    return ext or "unknown"


def read_document(data: bytes, name: str = "") -> tuple[str, list[Paragraph]]:
    kind = detect(data, name)
    try:
        if kind == "hwp":
            return kind, read_hwp(data)
        if kind == "hwpx":
            return kind, read_hwpx(data)
        if kind == "pdf":
            return kind, read_pdf(data)
    except HwpError as e:
        raise UnsupportedDocument(str(e)) from e
    raise UnsupportedDocument(f"unsupported document type: {kind}")
