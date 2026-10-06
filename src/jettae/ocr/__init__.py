"""Table-image / scanned-page transcription (optional; VLM calls are live-only and budgeted)."""

from jettae.ocr.base import (
    OcrCell,
    OcrFailed,
    OcrPage,
    OcrProvider,
    TableImage,
    TableTranscriber,
    TranscribedTable,
    TranscriptionMissing,
)
from jettae.ocr.manual import (
    ManualTranscriber,
    compare_tables,
    load_cells_csv,
    load_ftc_rows,
    write_cells_csv,
)
from jettae.ocr.vlm import VlmPdfOcr, VlmTableTranscriber, pdf_ocr_from_env

__all__ = [
    "ManualTranscriber",
    "OcrCell",
    "OcrFailed",
    "OcrPage",
    "OcrProvider",
    "TableImage",
    "TableTranscriber",
    "TranscribedTable",
    "TranscriptionMissing",
    "VlmPdfOcr",
    "VlmTableTranscriber",
    "compare_tables",
    "load_cells_csv",
    "load_ftc_rows",
    "pdf_ocr_from_env",
    "write_cells_csv",
]
