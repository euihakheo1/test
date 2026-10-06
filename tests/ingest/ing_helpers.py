"""Tiny hand-written fixtures for ingest tests (generated in memory; not evaluation data).

PDFs are written by hand (reportlab is not installed): a text PDF uses the non-embedded
predefined CJK font HYSMyeongJo-Medium with the UniKS-UCS2-H CMap (pdfminer ships the CMap),
so Korean text is extractable without any font file. Scanned PDFs are made with Pillow.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence

TENANT = "t1"


def build_pdf(page_streams: Sequence[bytes], *, with_font: bool = True) -> bytes:
    n = len(page_streams)
    # objects: 1 catalog, 2 pages, 3 font, 4 cidfont, 5 descriptor, then (page, content) pairs
    page_ids = [6 + 2 * i for i in range(n)]
    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids ["
        + b" ".join(b"%d 0 R" % p for p in page_ids)
        + b"] /Count %d >>" % n,
        3: b"<< /Type /Font /Subtype /Type0 /BaseFont /HYSMyeongJo-Medium "
        b"/Encoding /UniKS-UCS2-H /DescendantFonts [4 0 R] >>",
        4: b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HYSMyeongJo-Medium "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Korea1) /Supplement 1 >> "
        b"/FontDescriptor 5 0 R /DW 1000 >>",
        5: b"<< /Type /FontDescriptor /FontName /HYSMyeongJo-Medium /Flags 6 "
        b"/FontBBox [-28 -148 1001 880] /ItalicAngle 0 /Ascent 880 /Descent -120 "
        b"/CapHeight 880 /StemV 93 >>",
    }
    for i, stream in enumerate(page_streams):
        pid = page_ids[i]
        res = b"/Resources << /Font << /F1 3 0 R >> >>" if with_font else b"/Resources << >>"
        objs[pid] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            + res
            + b" /Contents %d 0 R >>" % (pid + 1)
        )
        objs[pid + 1] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for oid in sorted(objs):
        offsets[oid] = out.tell()
        out.write(b"%d 0 obj\n" % oid + objs[oid] + b"\nendobj\n")
    xref = out.tell()
    size = max(objs) + 1
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % size)
    for oid in range(1, size):
        out.write(b"%010d 00000 n \n" % offsets[oid])
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref))
    return out.getvalue()


def text_op(x: float, y: float, s: str, size: int = 9) -> bytes:
    hexs = s.encode("utf-16-be").hex().encode()
    return b"BT /F1 %d Tf %d %d Td <%s> Tj ET\n" % (size, x, y, hexs)


def grid_table(
    x0: int, y_top: int, col_widths: Sequence[int], rows: Sequence[Sequence[str]], h: int = 20
) -> bytes:
    """Ruled table: horizontal + vertical lines and one text run per cell."""
    ops = [b"0.5 w\n"]
    width = sum(col_widths)
    n = len(rows)
    for i in range(n + 1):
        y = y_top - i * h
        ops.append(b"%d %d m %d %d l S\n" % (x0, y, x0 + width, y))
    x = x0
    xs = [x0]
    for w in col_widths:
        x += w
        xs.append(x)
    for xv in xs:
        ops.append(b"%d %d m %d %d l S\n" % (xv, y_top, xv, y_top - n * h))
    for r, row in enumerate(rows):
        for c, txt in enumerate(row):
            if txt:
                ops.append(text_op(xs[c] + 3, y_top - (r + 1) * h + 6, txt))
    return b"".join(ops)


def scanned_pdf(pages: int = 1) -> bytes:
    from PIL import Image, ImageDraw

    imgs = []
    for i in range(pages):
        im = Image.new("RGB", (400, 300), "white")
        d = ImageDraw.Draw(im)
        d.rectangle([20, 20, 380, 80], outline="black")
        d.text((30, 40), f"scanned page {i + 1} 1,000,000", fill="black")
        imgs.append(im)
    buf = io.BytesIO()
    imgs[0].save(buf, format="PDF", save_all=True, append_images=imgs[1:], resolution=72)
    return buf.getvalue()


def add_zip_member(xlsx: bytes, name: str, data: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(xlsx))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr(name, data)
    return out.getvalue()


def save_wb(wb: object) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)  # type: ignore[attr-defined]
    return buf.getvalue()
