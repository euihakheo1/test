"""BPI Challenge 2019 download from 4TU.ResearchData (djehuty API) + manifest.

Discovery: DOI -> doi.org redirect -> 4TU article id -> ``/v2/articles/{id}/files`` ->
the ``BPI_Challenge_2019.xes`` entry (``download_url``, ``supplied_md5``, ``size``).
4TU sometimes answers ``{"status": "maintenance"}`` (or an HTML maintenance page): this is
detected, retried with exponential backoff and finally reported as unavailable — nothing is
faked. A file obtained elsewhere can be registered with ``register_local_file``.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from jettae.sources import (
    USER_AGENT,
    manifests_dir,
    raw_dir,
    read_json,
    repo_root,
    sha256_file,
    utc_now_iso,
    write_json,
)

DOI = "10.4121/uuid:d06aff4b-79f0-45e6-8ec8-e19730c248f1"
DOI_URL = f"https://doi.org/{DOI}"
API = "https://data.4tu.nl/v2"
FALLBACK_ARTICLE_ID = "12715853"  # doi.org redirect target observed 2026-10-06
LICENSE = "CC BY 4.0"
CITATION = "van Dongen, B.F. (2019). BPI Challenge 2019. 4TU.ResearchData. " + DOI_URL
FILE_RE = re.compile(r"^BPI_Challenge_2019\.xes(\.gz|\.zip)?$", re.IGNORECASE)
FETCH_CMD = "uv run jettae sources bpi2019 fetch"


class SourceUnavailable(RuntimeError):
    """The source could not be reached / is in maintenance (after retries)."""


class Maintenance(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteFile:
    name: str
    url: str
    size: int | None
    md5: str | None


def manifest_path() -> Path:
    return manifests_dir() / "bpi2019.json"


def load_manifest() -> dict[str, Any]:
    p = manifest_path()
    if p.exists():
        data: dict[str, Any] = read_json(p)
        return data
    return {
        "source": "bpi2019",
        "title": "BPI Challenge 2019",
        "doi": DOI,
        "landing_url": DOI_URL,
        "license": LICENSE,
        "citation": CITATION,
        "status": "not_fetched",
        "file": None,
        "attempts": [],
    }


def save_manifest(m: dict[str, Any]) -> Path:
    m["attempts"] = m.get("attempts", [])[-30:]
    return write_json(manifest_path(), m)


def _is_maintenance(resp: httpx.Response) -> bool:
    if resp.status_code == 503:
        return True
    ctype = resp.headers.get("content-type", "")
    text = resp.text[:20000] if ("json" in ctype or "html" in ctype or "text" in ctype) else ""
    if "json" in ctype or text.lstrip().startswith("{"):
        try:
            js = resp.json()
        except ValueError:
            js = None
        if isinstance(js, dict) and str(js.get("status", "")).lower() == "maintenance":
            return True
    return "offline for maintenance" in text.lower() or "<title>maintenance" in text.lower()


def resolve_article_id(client: httpx.Client) -> str:
    try:
        r = client.get(DOI_URL, follow_redirects=False)
        loc = r.headers.get("location", "")
        m = re.search(r"data\.4tu\.nl/(?:articles|datasets)/[^/]+/(\d+)", loc)
        if m:
            return m.group(1)
    except httpx.HTTPError:
        pass
    return FALLBACK_ARTICLE_ID


def list_files(client: httpx.Client, article_id: str) -> list[RemoteFile]:
    url = f"{API}/articles/{article_id}/files"
    r = client.get(url, headers={"Accept": "application/json"})
    if _is_maintenance(r):
        raise Maintenance(f"{url} -> maintenance")
    r.raise_for_status()
    try:
        js = r.json()
    except ValueError as e:
        raise RuntimeError(f"{url}: unexpected non-JSON answer") from e
    out: list[RemoteFile] = []
    for f in js if isinstance(js, list) else []:
        if not isinstance(f, dict) or not f.get("download_url"):
            continue
        size = f.get("size")
        out.append(
            RemoteFile(
                str(f.get("name", "")),
                str(f["download_url"]),
                int(size) if isinstance(size, int) else None,
                (f.get("supplied_md5") or f.get("computed_md5") or None),
            )
        )
    return out


def pick_file(files: list[RemoteFile]) -> RemoteFile | None:
    exact = [f for f in files if FILE_RE.match(f.name)]
    if exact:
        return sorted(exact, key=lambda f: f.name)[0]
    xes = [f for f in files if ".xes" in f.name.lower()]
    return max(xes, key=lambda f: f.size or 0) if xes else None


def _download(client: httpx.Client, rf: RemoteFile, dest: Path) -> tuple[str, str, int]:
    tmp = dest.with_suffix(dest.suffix + ".part")
    sha, md5, n = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    with client.stream("GET", rf.url, follow_redirects=True) as r:
        if _is_maintenance_head(r):
            raise Maintenance(f"{rf.url} -> maintenance")
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                sha.update(chunk)
                md5.update(chunk)
                n += len(chunk)
    if rf.md5 and md5.hexdigest().lower() != rf.md5.lower():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"md5 mismatch for {rf.name}: got {md5.hexdigest()}, expected {rf.md5}")
    if rf.size and n != rf.size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"size mismatch for {rf.name}: got {n}, expected {rf.size}")
    tmp.replace(dest)
    return sha.hexdigest(), md5.hexdigest(), n


def _is_maintenance_head(r: httpx.Response) -> bool:
    if r.status_code == 503:
        return True
    ctype = r.headers.get("content-type", "")
    if "json" in ctype or "html" in ctype:
        r.read()
        return _is_maintenance(r)
    return False


def _md5_file(path: Path) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(p: Path) -> str:
    try:
        return p.resolve().relative_to(repo_root().resolve()).as_posix()
    except ValueError:
        return str(p)


def fetch(
    *,
    retries: int = 4,
    backoff_s: float = 30.0,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    dest_dir: Path | None = None,
    log: Callable[[str], None] = print,
) -> Path:
    """Discover and download the XES file. Raises :class:`SourceUnavailable` after retries."""
    m = load_manifest()
    own = client is None
    cl = client or httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(60.0, read=300.0)
    )
    dest_dir = dest_dir or raw_dir("bpi2019")
    last = ""
    try:
        for attempt in range(1, retries + 1):
            rec: dict[str, Any] = {"at": utc_now_iso(), "attempt": attempt}
            try:
                aid = resolve_article_id(cl)
                rec["article_id"] = aid
                files = list_files(cl, aid)
                rf = pick_file(files)
                if rf is None:
                    raise RuntimeError(
                        "file list has no BPI_Challenge_2019.xes entry: "
                        + ", ".join(f.name for f in files)
                    )
                dest = dest_dir / rf.name
                if dest.exists() and rf.md5:
                    md5 = _md5_file(dest)
                    if md5.lower() == rf.md5.lower():
                        sha, n = sha256_file(dest), dest.stat().st_size
                        log(f"already present and md5 verified: {dest}")
                    else:
                        sha, md5, n = _download(cl, rf, dest)
                else:
                    log(f"downloading {rf.url} ({rf.size or '?'} bytes) -> {dest}")
                    sha, md5, n = _download(cl, rf, dest)
                rec.update(status="ok", url=rf.url)
                m["attempts"] = m.get("attempts", []) + [rec]
                m["status"] = "ok"
                m["file"] = {
                    "name": rf.name,
                    "url": rf.url,
                    "size": n,
                    "sha256": sha,
                    "md5": md5,
                    "supplied_md5": rf.md5,
                    "path": _rel(dest),
                    "fetched_at": utc_now_iso(),
                    "origin": "4tu",
                }
                save_manifest(m)
                return dest
            except Maintenance as e:
                last = f"4TU.ResearchData is in maintenance ({e})"
                rec.update(status="maintenance", note=str(e))
            except (httpx.HTTPError, RuntimeError) as e:
                last = f"{type(e).__name__}: {e}"
                rec.update(status="error", note=last)
            m["attempts"] = m.get("attempts", []) + [rec]
            if attempt < retries:
                wait = backoff_s * (2 ** (attempt - 1))
                log(f"attempt {attempt}/{retries} failed: {last}; retrying in {wait:.0f}s")
                sleep(wait)
        if m.get("status") != "ok":
            m["status"] = "unavailable"
        save_manifest(m)
        raise SourceUnavailable(
            f"BPI Challenge 2019 could not be downloaded after {retries} attempts: {last}.\n"
            f"Retry later with:  {FETCH_CMD}\n"
            f"or register a copy downloaded from {DOI_URL} with:  "
            f"{FETCH_CMD} --file <path/to/BPI_Challenge_2019.xes>"
        )
    finally:
        if own:
            cl.close()


class ProvenanceMismatch(RuntimeError):
    """A local file whose md5 differs from the published 4TU checksum."""


def _published_md5(client: httpx.Client | None = None) -> tuple[str | None, str]:
    """(md5 published in the 4TU file list, note). Never raises: (None, why) if unreachable."""
    own = client is None
    cl = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0)
    try:
        rf = pick_file(list_files(cl, resolve_article_id(cl)))
        if rf is None or not rf.md5:
            return None, "4TU file list has no md5 for BPI_Challenge_2019.xes"
        return rf.md5.lower(), "md5 from the 4TU file list"
    except Maintenance as e:
        return None, f"4TU in maintenance ({e})"
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as e:
        return None, f"4TU file list not reachable ({type(e).__name__}: {e})"
    finally:
        if own:
            cl.close()


def register_local_file(
    path: Path,
    *,
    expected_md5: str | None = None,
    check_remote: bool = True,
    client: httpx.Client | None = None,
) -> Path:
    """Record a user-provided copy of the log (hash + size) in the manifest.

    Provenance: the file's md5 is compared with the md5 published by 4TU (when reachable)
    and/or ``expected_md5``. A mismatch is refused. If nothing could be compared, the file is
    registered with status ``unverified_local`` and every E4/E6 output says so."""
    if not path.exists():
        raise FileNotFoundError(path)
    md5 = _md5_file(path)
    published, note = (None, "remote check skipped")
    if check_remote:
        published, note = _published_md5(client)
    for ref, label in ((published, "4TU"), (expected_md5, "--md5")):
        if ref and ref.lower() != md5.lower():
            raise ProvenanceMismatch(
                f"{path} md5 {md5} differs from the {label} checksum {ref}: "
                "not the published BPI_Challenge_2019.xes"
            )
    if published:
        status, verified = "local_file", "md5 equals the 4TU published checksum"
    elif expected_md5:
        status, verified = "local_file", "md5 equals the checksum given with --md5 (user)"
    else:
        status, verified = "unverified_local", f"md5 NOT compared with 4TU: {note}"
    m = load_manifest()
    m["status"] = status
    m["file"] = {
        "name": path.name,
        "url": None,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "md5": md5,
        "supplied_md5": published or expected_md5,
        "provenance": verified,
        "path": _rel(path),
        "fetched_at": utc_now_iso(),
        "origin": "user-provided local file (not downloaded by jettae)",
    }
    m["attempts"] = m.get("attempts", []) + [
        {"at": utc_now_iso(), "status": status, "path": _rel(path), "note": verified}
    ]
    save_manifest(m)
    return path


def provenance_warning(m: dict[str, Any] | None = None) -> str | None:
    """None when the registered log was downloaded from 4TU or its md5 was verified;
    otherwise a label that outputs must show next to any number."""
    m = m if m is not None else load_manifest()
    if m.get("status") == "unverified_local":
        f = m.get("file") or {}
        return f"unverified local file ({f.get('provenance') or 'md5 not checked'})"
    return None


def local_log_path() -> Path | None:
    """Path of the registered/downloaded log if it exists on disk."""
    m = load_manifest()
    f = m.get("file") or {}
    p = f.get("path")
    if not p:
        return None
    path = Path(p)
    if not path.is_absolute():
        path = repo_root() / path
    return path if path.exists() else None
