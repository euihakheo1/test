"""Upload checks: size limit while streaming, extension + magic-byte MIME detection, and
filename sanitising. The client's declared Content-Type is never trusted on its own."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from fastapi import UploadFile

from jettae.api.errors import ApiError

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MEDIA = {
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".xlsx": XLSX,
    ".pdf": "application/pdf",
}
# declared types we accept for each extension (browsers/OSes differ)
DECLARED_OK = {
    ".csv": {"text/csv", "application/csv", "text/plain", "application/vnd.ms-excel"},
    ".txt": {"text/plain"},
    ".xlsx": {XLSX, "application/zip"},
    ".pdf": {"application/pdf"},
}
GENERIC = {"", "application/octet-stream", "binary/octet-stream"}
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class CheckedUpload:
    filename: str
    media_type: str
    content: bytes


def sanitize_filename(name: str | None) -> str:
    raw = unicodedata.normalize("NFC", name or "")
    base = re.split(r"[\\/]", raw)[-1]
    base = _CTRL.sub("", base).strip().strip(".")
    base = base.replace('"', "").replace(":", "_")
    if len(base) > 200:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: 200 - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else base[:200]
    return base or "upload"


def _extension(name: str) -> str:
    _, dot, ext = name.rpartition(".")
    return ("." + ext.lower()) if dot else ""


def sniff(ext: str, head: bytes) -> bool:
    if ext == ".xlsx":
        return head.startswith(b"PK\x03\x04")
    if ext == ".pdf":
        return head.lstrip(b"\r\n\t ").startswith(b"%PDF-")
    if ext in (".csv", ".txt"):
        if head.startswith((b"PK\x03\x04", b"%PDF-", b"\xd0\xcf\x11\xe0", b"MZ")):
            return False
        utf16 = head.startswith((b"\xff\xfe", b"\xfe\xff"))
        return utf16 or b"\x00" not in head
    return False


async def read_limited(file: UploadFile, max_bytes: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise ApiError(
                413,
                "payload_too_large",
                f"file exceeds the upload limit of {max_bytes} bytes",
                {"max_bytes": max_bytes},
            )
    return bytes(buf)


async def check_upload(
    file: UploadFile, *, max_bytes: int, allowed_ext: tuple[str, ...]
) -> CheckedUpload:
    name = sanitize_filename(file.filename)
    ext = _extension(name)
    if ext not in allowed_ext or ext not in MEDIA:
        raise ApiError(
            415,
            "unsupported_media_type",
            f"file type not accepted; allowed: {', '.join(allowed_ext)}",
            {"extension": ext or None},
        )
    declared = (file.content_type or "").split(";")[0].strip().lower()
    if declared not in GENERIC and declared not in DECLARED_OK[ext]:
        raise ApiError(
            415,
            "unsupported_media_type",
            f"declared content type {declared!r} does not match a {ext} file",
        )
    content = await read_limited(file, max_bytes)
    if not content:
        raise ApiError(422, "empty_file", "the uploaded file is empty")
    if not sniff(ext, content[:8192]):
        raise ApiError(
            415,
            "unsupported_media_type",
            f"file content does not look like a {ext} file",
        )
    return CheckedUpload(name, MEDIA[ext], content)
