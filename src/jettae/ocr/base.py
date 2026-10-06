"""OCR / transcription interfaces.

Two consumers:
- ingest (scanned PDF pages): the :class:`OcrProvider` port defined in
  :mod:`jettae.ingest.ocrhook` (re-exported here); :class:`jettae.ocr.vlm.VlmPdfOcr`
  implements it;
- sources.ftc (공정위 의결서 table images): :class:`TableTranscriber`, implemented by the VLM
  (:mod:`jettae.ocr.vlm`) and by human/agent transcription CSVs (:mod:`jettae.ocr.manual`).

A transcription is never verified data: every :class:`TranscribedTable` carries who/what
produced it (transcriber, model, run id, prompt hash, image sha256) and ``verified=False``
until a person checks it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from jettae.ingest.ocrhook import OcrCell, OcrFailed, OcrPage, OcrProvider

__all__ = [
    "OcrCell",
    "OcrFailed",
    "OcrPage",
    "OcrProvider",
    "TableImage",
    "TableTranscriber",
    "TranscribedTable",
    "TranscriptionMissing",
]


class TranscriptionMissing(LookupError):
    pass


@dataclass(frozen=True)
class TableImage:
    data: bytes
    source_id: str  # e.g. FTC flSeq
    media_type: str = "image/png"
    context: Mapping[str, Any] = field(default_factory=dict)  # decision id, caption, unit...

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class IllegibleCell:
    row: int  # 0-based body row index
    col: int
    reason: str


@dataclass(frozen=True)
class TranscribedTable:
    source_id: str
    image_sha256: str
    header_rows: tuple[tuple[str, ...], ...]
    rows: tuple[tuple[str, ...], ...]
    transcriber: str  # "vlm:<model>" | "manual:<who>"
    title: str | None = None
    unit_note: str | None = None
    notes: tuple[str, ...] = ()
    illegible: tuple[IllegibleCell, ...] = ()
    model: str | None = None
    run_id: str | None = None
    prompt_hash: str | None = None
    verified: bool = False
    meta: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class TableTranscriber(Protocol):
    name: str

    def transcribe(self, image: TableImage) -> TranscribedTable: ...
