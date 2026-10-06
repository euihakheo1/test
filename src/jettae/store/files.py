"""Per-tenant content-addressed blob store on local disk (implements ``app.ports.BlobStore``).

Layout: ``<root>/<tenant dir>/<aa>/<sha256>`` where ``<tenant dir>`` is
``t_`` + sha256(tenant_id)[:32]. Keys are the sha256 hex of the content and are only ever
resolved inside the caller's tenant directory, so

* identical files of two tenants are stored twice (no cross-tenant deduplication, so a
  tenant can never learn whether another tenant holds a given file);
* a key of tenant A looked up as tenant B returns ``None``;
* keys are validated (64 lowercase hex chars) — no path traversal.

Writes are atomic (temp file + ``os.replace``); reads re-verify the content hash.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path

_KEY = re.compile(r"^[0-9a-f]{64}$")


class BlobIntegrityError(RuntimeError):
    """Stored bytes no longer match their content hash (disk corruption / tampering)."""


class FileBlobStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths ---------------------------------------------------------------------------
    @staticmethod
    def tenant_dir_name(tenant_id: str) -> str:
        if not tenant_id:
            raise ValueError("tenant_id required")
        return "t_" + hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:32]

    def _tenant_root(self, tenant_id: str) -> Path:
        return self.root / self.tenant_dir_name(tenant_id)

    def _path(self, tenant_id: str, key: str) -> Path:
        if not isinstance(key, str) or not _KEY.match(key):
            raise ValueError("invalid blob key")
        return self._tenant_root(tenant_id) / key[:2] / key

    # -- BlobStore port ---------------------------------------------------------------------
    def put(self, tenant_id: str, content: bytes) -> str:
        key = hashlib.sha256(content).hexdigest()
        path = self._path(tenant_id, key)
        if path.exists() and path.stat().st_size == len(content):
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        return key

    def get(self, tenant_id: str, key: str) -> bytes | None:
        try:
            path = self._path(tenant_id, key)
        except ValueError:
            return None
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        if hashlib.sha256(data).hexdigest() != key:
            raise BlobIntegrityError(f"blob {key[:12]}... of tenant failed integrity check")
        return data

    # -- extras ---------------------------------------------------------------------------
    def exists(self, tenant_id: str, key: str) -> bool:
        try:
            return self._path(tenant_id, key).is_file()
        except ValueError:
            return False

    def delete(self, tenant_id: str, key: str) -> bool:
        try:
            self._path(tenant_id, key).unlink()
            return True
        except (FileNotFoundError, ValueError):
            return False

    def keys(self, tenant_id: str) -> Iterator[str]:
        base = self._tenant_root(tenant_id)
        if not base.exists():
            return
        for p in sorted(base.glob("??/*")):
            if _KEY.match(p.name):
                yield p.name

    def writable(self) -> bool:
        try:
            fd, tmp = tempfile.mkstemp(prefix=".probe-", dir=self.root)
            os.close(fd)
            os.unlink(tmp)
            return True
        except OSError:
            return False
