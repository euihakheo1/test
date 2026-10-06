"""법제처 DRF Open API client for 공정위 의결서 (``target=ftc``).

- ``OC`` comes from ``JETTAE_DRF_OC`` (default ``test`` = DRF sample key, development only;
  production needs your own OC registered at https://open.law.go.kr).
- Polite: a minimum interval between network requests, bounded retries with exponential
  backoff on transport errors / 429 / 5xx.
- Every response that is used is cached on disk under ``data/raw/ftc`` (decisions:
  ``xml/<id>.xml``, images: ``img/<flSeq>.png``, searches: ``search/<key>.json``).
- URLs written to manifests never contain the OC value (``OC={OC}`` placeholder).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from jettae.sources.ftc.paths import long_path, raw_dir

DRF_BASE = "https://www.law.go.kr/DRF"
FILE_URL = "https://www.law.go.kr/LSW/flDownload.do"
DEFAULT_OC = "test"
USER_AGENT = "jettae-ftc-collector/0.1 (+research; polite rate limit)"


class DrfError(RuntimeError):
    """DRF answered, but not with the expected content (bad OC, missing id, HTML error page)."""


def drf_oc() -> str:
    return os.environ.get("JETTAE_DRF_OC", DEFAULT_OC) or DEFAULT_OC


def public_url(base: str, params: dict[str, str]) -> str:
    """URL with the OC value replaced by a placeholder (safe for manifests)."""
    shown = {k: ("{OC}" if k == "OC" else v) for k, v in params.items()}
    return f"{base}?{urlencode(shown, safe='{}')}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class SearchHit:
    decision_id: str
    case_no: str
    title: str
    decision_no: str
    decision_date: str
    doc_type: str
    meeting: str

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SearchHit:
        return cls(
            decision_id=str(d.get("결정문일련번호", "")).strip(),
            case_no=str(d.get("사건번호", "")).strip(),
            title=str(d.get("사건명", "")).strip(),
            decision_no=re.sub(r"\s+", " ", str(d.get("결정번호", ""))).strip(),
            decision_date=str(d.get("결정일자", "")).strip(),
            doc_type=str(d.get("문서유형", "")).strip(),
            meeting=re.sub(r"\s+", " ", str(d.get("회의종류", ""))).strip(),
        )


@dataclass(frozen=True)
class SearchPage:
    total: int
    page: int
    hits: list[SearchHit]


@dataclass(frozen=True)
class Fetched:
    """A fetched (or cached) resource."""

    key: str
    url: str  # public URL (OC redacted)
    path: Path
    sha256: str
    size: int
    fetched_at: str  # ISO UTC time of the network fetch (from cache metadata when cached)
    from_cache: bool
    content_type: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def read_bytes(self) -> bytes:
        return long_path(self.path).read_bytes()


class DrfClient:
    """Small synchronous DRF client with rate limiting, retries and an on-disk cache."""

    def __init__(
        self,
        oc: str | None = None,
        cache_dir: Path | None = None,
        *,
        min_interval: float = 1.0,
        max_retries: int = 4,
        backoff: float = 2.0,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        offline: bool = False,
    ) -> None:
        self.oc = oc or drf_oc()
        self.cache_dir = cache_dir or raw_dir()
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff = backoff
        self.offline = offline
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self.network_requests = 0
        self._http = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            transport=transport,
        )

    # ------------------------------------------------------------ plumbing
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DrfClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _wait_turn(self) -> None:
        if self._last is not None:
            gap = self._clock() - self._last
            if gap < self.min_interval:
                self._sleep(self.min_interval - gap)
        self._last = self._clock()

    def _get(self, url: str, params: dict[str, str]) -> httpx.Response:
        if self.offline:
            raise DrfError(f"offline mode: not fetching {public_url(url, params)}")
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            self.network_requests += 1
            try:
                resp = self._http.get(url, params=params)
            except httpx.TransportError as e:
                last_exc = e
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = DrfError(f"HTTP {resp.status_code} for {public_url(url, params)}")
                elif resp.status_code >= 400:
                    raise DrfError(f"HTTP {resp.status_code} for {public_url(url, params)}")
                else:
                    return resp
            if attempt < self.max_retries:
                self._sleep(self.backoff * (2**attempt))
        assert last_exc is not None
        if isinstance(last_exc, DrfError):
            raise last_exc
        raise DrfError(f"network error for {public_url(url, params)}: {last_exc!r}") from last_exc

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def _meta_path(self, path: Path) -> Path:
        return path.with_name(path.stem + ".m.json")

    def _cached(self, key: str, path: Path, url: str) -> Fetched | None:
        if not long_path(path).exists():
            return None
        data = long_path(path).read_bytes()
        meta: dict[str, Any] = {}
        mp = self._meta_path(path)
        if long_path(mp).exists():
            meta = json.loads(long_path(mp).read_text(encoding="utf-8"))
        return Fetched(
            key=key,
            url=meta.get("url", url),
            path=path,
            sha256=sha256(data),
            size=len(data),
            fetched_at=str(meta.get("fetched_at", "")),
            from_cache=True,
            content_type=str(meta.get("content_type", "")),
            meta=meta,
        )

    def _store(
        self, key: str, path: Path, url: str, data: bytes, content_type: str, **extra: Any
    ) -> Fetched:
        long_path(path.parent).mkdir(parents=True, exist_ok=True)
        tmp = long_path(path.with_name(path.name + ".part"))
        tmp.write_bytes(data)
        tmp.replace(long_path(path))
        meta = {"url": url, "fetched_at": self._now(), "content_type": content_type, **extra}
        long_path(self._meta_path(path)).write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return Fetched(
            key=key,
            url=url,
            path=path,
            sha256=sha256(data),
            size=len(data),
            fetched_at=meta["fetched_at"],
            from_cache=False,
            content_type=content_type,
            meta=meta,
        )

    # ------------------------------------------------------------ API
    def search(
        self,
        query: str,
        *,
        page: int = 1,
        display: int = 100,
        body: bool = False,
        sort: str | None = None,
        refresh: bool = False,
    ) -> SearchPage:
        """One search page. ``body=True`` searches the decision body (``search=2``)."""
        params = {
            "OC": self.oc,
            "target": "ftc",
            "type": "JSON",
            "query": query,
            "display": str(display),
            "page": str(page),
            "search": "2" if body else "1",
        }
        if sort:
            params["sort"] = sort
        url = f"{DRF_BASE}/lawSearch.do"
        pub = public_url(url, params)
        key = sha256(pub.encode("utf-8"))[:12]
        path = self.cache_dir / "q" / f"{key}.json"
        hit = None if refresh else self._cached(key, path, pub)
        if hit is None:
            resp = self._get(url, params)
            hit = self._store(key, path, pub, resp.content, resp.headers.get("content-type", ""))
        return parse_search(hit.read_bytes(), page)

    def search_all(
        self,
        query: str,
        *,
        body: bool = False,
        cap: int | None = None,
        sort: str | None = None,
        refresh: bool = False,
        display: int = 100,
    ) -> tuple[int, list[SearchHit]]:
        """All hits (deduplicated by decision id, in API order), up to ``cap``."""
        first = self.search(query, page=1, display=display, body=body, sort=sort, refresh=refresh)
        hits = list(first.hits)
        page = 1
        limit = first.total if cap is None else min(cap, first.total)
        while len(hits) < limit and len(hits) < first.total:
            page += 1
            nxt = self.search(
                query, page=page, display=display, body=body, sort=sort, refresh=refresh
            )
            if not nxt.hits:
                break
            hits.extend(nxt.hits)
        seen: set[str] = set()
        out: list[SearchHit] = []
        for h in hits:
            if h.decision_id and h.decision_id not in seen:
                seen.add(h.decision_id)
                out.append(h)
        return first.total, out[:limit] if cap is not None else out

    def fetch_decision(self, decision_id: str, *, refresh: bool = False) -> Fetched:
        if not re.fullmatch(r"\d+", decision_id):
            raise ValueError(f"invalid decision id {decision_id!r}")
        params = {"OC": self.oc, "target": "ftc", "ID": decision_id, "type": "XML"}
        url = f"{DRF_BASE}/lawService.do"
        pub = public_url(url, params)
        path = self.cache_dir / "xml" / f"{decision_id}.xml"
        if not refresh and (c := self._cached(decision_id, path, pub)) is not None:
            return c
        resp = self._get(url, params)
        check_decision_xml(resp.content, decision_id)
        return self._store(
            decision_id, path, pub, resp.content, resp.headers.get("content-type", "")
        )

    def fetch_image(self, flseq: str, *, refresh: bool = False) -> Fetched:
        if not re.fullmatch(r"\d+", flseq):
            raise ValueError(f"invalid flSeq {flseq!r}")
        params = {"flSeq": flseq}
        pub = public_url(FILE_URL, params)
        path = self.cache_dir / "img" / f"{flseq}.png"
        if not refresh:
            for suffix in (".png", ".jpg", ".gif"):
                if (c := self._cached(flseq, path.with_suffix(suffix), pub)) is not None:
                    return c
        resp = self._get(FILE_URL, params)
        ctype = resp.headers.get("content-type", "")
        if not resp.content.startswith(b"\x89PNG") and "image" not in ctype:
            raise DrfError(f"flSeq={flseq}: not an image (content-type {ctype!r})")
        suffix = ".png"
        if resp.content.startswith(b"\xff\xd8"):
            suffix = ".jpg"
        elif resp.content.startswith(b"GIF8"):
            suffix = ".gif"
        path = path.with_suffix(suffix)
        return self._store(
            flseq,
            path,
            pub,
            resp.content,
            ctype,
            content_disposition=resp.headers.get("content-disposition", ""),
        )


def parse_search(data: bytes, page: int = 1) -> SearchPage:
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise DrfError(f"search: response is not JSON ({data[:120]!r})") from e
    root = doc.get("Ftc") if isinstance(doc, dict) else None
    if not isinstance(root, dict):
        raise DrfError(f"search: unexpected response {str(doc)[:200]}")
    items = root.get("ftc", [])
    if isinstance(items, dict):  # a single hit is returned as an object
        items = [items]
    total = int(str(root.get("totalCnt", "0")) or 0)
    return SearchPage(total=total, page=page, hits=[SearchHit.from_json(i) for i in items])


def check_decision_xml(data: bytes, decision_id: str) -> None:
    """Raise :class:`DrfError` unless ``data`` is an ``FtcService`` XML for ``decision_id``."""
    head = data[:4000].decode("utf-8", errors="replace")
    if "<FtcService" not in head:
        m = re.search(r"<result>(.*?)</result>", head, re.S)
        reason = m.group(1).strip() if m else head.strip()[:160]
        raise DrfError(f"decision {decision_id}: not an FtcService XML ({reason})")
    m = re.search(r"<결정문일련번호>\s*(\d+)\s*</결정문일련번호>", head)
    if m and m.group(1) != decision_id:
        raise DrfError(f"decision {decision_id}: response is for id {m.group(1)}")
