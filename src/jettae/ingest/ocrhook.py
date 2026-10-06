"""OCR port used by the PDF parser for scanned pages. Implementations live in ``jettae.ocr``.

Without a provider, a scanned PDF is reported as ``UNSUPPORTED_SCAN`` (never as "no rows").
A provider failure raises :class:`OcrFailed`, recorded as ``FAILED`` with code ``ocr_failed``.
OCR output is transcription, not verified data: cells carry ``"ocr": <provider>`` in their
locator so downstream checks can tell them apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from jettae.domain.status import DocumentStatus
from jettae.ingest.cells import IngestError


@dataclass(frozen=True)
class OcrCell:
    text: str
    bbox: tuple[int, int, int, int] | None = None  # integer PDF points (x0, top, x1, bottom)


@dataclass(frozen=True)
class OcrPage:
    page: int  # 1-based page number
    text: str  # full page transcription
    tables: Sequence[Sequence[Sequence[OcrCell]]] = field(default_factory=tuple)
    provider: str = ""


@runtime_checkable
class OcrProvider(Protocol):
    name: str

    def ocr_pdf_pages(self, content: bytes, pages: Sequence[int]) -> Sequence[OcrPage]:
        """Transcribe the given 1-based pages of a PDF. Raise :class:`OcrFailed` on failure."""
        ...


class OcrFailed(IngestError):
    status = DocumentStatus.FAILED

    def __init__(self, message: str) -> None:
        super().__init__(message, code="ocr_failed")
