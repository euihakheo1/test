"""Human/agent transcriptions loaded from CSV, written back, and compared cell by cell.

Two CSV shapes:

- **cells** (long format, written by ``jettae ocr ftc-tables``; one row per cell):
  ``source_id, image_sha256, part (header|body), row, col, text, transcriber, model, run_id,
  prompt_hash, verified, title, unit_note, illegible``.
- **ftc_rows** (``data/seeds/ftc_rows.csv`` from the FTC task; one row per table row with
  normalised columns). :func:`load_ftc_rows` groups it by ``flseq`` keeping provenance.

:class:`ManualTranscriber` serves loaded transcriptions through the same
:class:`TableTranscriber` interface as the VLM (lookup by source id / image hash).
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from jettae.ocr.base import (
    IllegibleCell,
    TableImage,
    TranscribedTable,
    TranscriptionMissing,
)

CELL_COLUMNS = (
    "source_id",
    "image_sha256",
    "part",
    "row",
    "col",
    "text",
    "transcriber",
    "model",
    "run_id",
    "prompt_hash",
    "verified",
    "title",
    "unit_note",
    "illegible",
)


def write_cells_csv(path: Path, tables: Iterable[TranscribedTable]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CELL_COLUMNS)
        w.writeheader()
        for t in tables:
            bad = {(c.row, c.col) for c in t.illegible}
            for part, grid in (("header", t.header_rows), ("body", t.rows)):
                for r, row in enumerate(grid):
                    for c, text in enumerate(row):
                        w.writerow(
                            {
                                "source_id": t.source_id,
                                "image_sha256": t.image_sha256,
                                "part": part,
                                "row": r,
                                "col": c,
                                "text": text,
                                "transcriber": t.transcriber,
                                "model": t.model or "",
                                "run_id": t.run_id or "",
                                "prompt_hash": t.prompt_hash or "",
                                "verified": "yes" if t.verified else "no",
                                "title": t.title or "",
                                "unit_note": t.unit_note or "",
                                "illegible": "yes" if part == "body" and (r, c) in bad else "",
                            }
                        )
                        n += 1
    return n


def load_cells_csv(path: Path) -> dict[str, TranscribedTable]:
    cells: dict[str, dict[str, dict[tuple[int, int], str]]] = defaultdict(
        lambda: {"header": {}, "body": {}}
    )
    meta: dict[str, dict[str, str]] = {}
    illegible: dict[str, list[IllegibleCell]] = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            sid = row["source_id"]
            r, c = int(row["row"]), int(row["col"])
            cells[sid][row["part"]][(r, c)] = row["text"]
            meta.setdefault(sid, row)
            if row.get("illegible") == "yes":
                illegible[sid].append(IllegibleCell(r, c, "marked illegible"))
    out = {}
    for sid, parts in cells.items():
        m = meta[sid]
        out[sid] = TranscribedTable(
            source_id=sid,
            image_sha256=m["image_sha256"],
            header_rows=_to_grid(parts["header"]),
            rows=_to_grid(parts["body"]),
            transcriber=m["transcriber"],
            title=m.get("title") or None,
            unit_note=m.get("unit_note") or None,
            illegible=tuple(illegible[sid]),
            model=m.get("model") or None,
            run_id=m.get("run_id") or None,
            prompt_hash=m.get("prompt_hash") or None,
            verified=m.get("verified") == "yes",
        )
    return out


def _to_grid(d: Mapping[tuple[int, int], str]) -> tuple[tuple[str, ...], ...]:
    if not d:
        return ()
    nrows = max(r for r, _ in d) + 1
    grid = []
    for r in range(nrows):
        cols = [c for rr, c in d if rr == r]
        width = max(cols) + 1 if cols else 0
        grid.append(tuple(d.get((r, c), "") for c in range(width)))
    return tuple(grid)


def load_ftc_rows(path: Path) -> dict[str, list[dict[str, str]]]:
    """``ftc_rows.csv`` grouped by ``flseq`` (rows in ``row_idx`` order)."""
    by: dict[str, list[dict[str, str]]] = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            by[row["flseq"]].append(dict(row))
    for rows in by.values():
        rows.sort(key=lambda r: int(r.get("row_idx") or 0))
    return dict(by)


def ftc_rows_as_table(
    flseq: str, rows: list[dict[str, str]], *, image_sha256: str = ""
) -> TranscribedTable:
    cols = [c for c in rows[0] if c] if rows else []
    return TranscribedTable(
        source_id=flseq,
        image_sha256=image_sha256,
        header_rows=(tuple(cols),),
        rows=tuple(tuple(r.get(c, "") for c in cols) for r in rows),
        transcriber="manual:" + (rows[0].get("transcriber", "") if rows else ""),
        title=rows[0].get("table_label") if rows else None,
        unit_note=rows[0].get("unit_note") if rows else None,
        verified=all(r.get("verified") == "yes" for r in rows) if rows else False,
        meta={"format": "ftc_rows"},
    )


class ManualTranscriber:
    """Serves existing transcriptions; never invents one for an unknown image."""

    def __init__(self, tables: Mapping[str, TranscribedTable], *, check_hash: bool = True):
        self.tables = dict(tables)
        self.check_hash = check_hash
        self.name = "manual"

    def transcribe(self, image: TableImage) -> TranscribedTable:
        t = self.tables.get(image.source_id)
        if t is None:
            raise TranscriptionMissing(f"no transcription for {image.source_id}")
        if self.check_hash and t.image_sha256 and t.image_sha256 != image.sha256:
            raise TranscriptionMissing(
                f"transcription of {image.source_id} was made for another image "
                f"({t.image_sha256[:12]} != {image.sha256[:12]})"
            )
        return t


# ----------------------------------------------------------------------------- comparison
_SPACE = re.compile(r"\s+")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def normalize_cell(s: str) -> str:
    """Whitespace removed, thousands separators removed (only between digit groups)."""
    return _THOUSANDS.sub("", _SPACE.sub("", s))


def compare_tables(a: TranscribedTable, b: TranscribedTable) -> dict[str, Any]:
    """Cell agreement of two transcriptions of the same image (body rows only)."""
    total = agree = 0
    diffs: list[dict[str, Any]] = []
    for r in range(max(len(a.rows), len(b.rows))):
        ra = a.rows[r] if r < len(a.rows) else ()
        rb = b.rows[r] if r < len(b.rows) else ()
        for c in range(max(len(ra), len(rb))):
            va = ra[c] if c < len(ra) else None
            vb = rb[c] if c < len(rb) else None
            total += 1
            if va is not None and vb is not None and normalize_cell(va) == normalize_cell(vb):
                agree += 1
            elif len(diffs) < 50:
                diffs.append({"row": r, "col": c, "a": va, "b": vb})
    return {
        "source_id": a.source_id,
        "same_image": a.image_sha256 == b.image_sha256
        if a.image_sha256 and b.image_sha256
        else None,
        "rows_a": len(a.rows),
        "rows_b": len(b.rows),
        "cells": total,
        "agree": agree,
        "agreement": f"{agree}/{total}",
        "diffs": diffs,
    }
