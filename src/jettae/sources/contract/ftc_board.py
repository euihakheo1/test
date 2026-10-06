"""공정위 표준유통거래계약서 board (www.ftc.go.kr, bordCd=204, key=205): list, attachments,
download + manifest (``data/manifests/contract.json``).

Only posts whose title names 직매입, 특약매입 or 위수탁 are fetched (대규모유통업법 제8조
trade forms). The preferred attachment format is PDF > HWPX > HWP (the board currently
offers HWP only). Failures are recorded per post, never hidden.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from lxml import html as lhtml

from jettae.sources import (
    USER_AGENT,
    manifests_dir,
    raw_dir,
    read_json,
    sha256_bytes,
    utc_now_iso,
    write_json,
)

BASE = "https://www.ftc.go.kr/www/"
LIST_URL = (
    BASE + "selectBbsNttList.do?pageUnit=10&searchCnd=all&key=205&bordCd=204&pageIndex={page}"
)
VIEW_URL = BASE + "selectBbsNttView.do?key=205&bordCd=204&nttSn={ntt}"
BOARD_NOTE = (
    "게시판 안내문: 표준계약서 서식은 저작권법 제24조의2(공공저작물의 자유이용)에 따라 자유롭게 "
    "이용할 수 있다고 표시됨(2026-10-06 확인)."
)
TITLE_RE = re.compile(r"직매입|특약매입|위수탁")
FORMAT_RANK = {"pdf": 0, "hwpx": 1, "hwp": 2}


@dataclass
class Post:
    ntt: str
    title: str
    registered: str | None
    url: str


@dataclass
class Attachment:
    file_no: str
    name: str
    ext: str
    size_text: str
    url: str


@dataclass
class FetchedContract:
    post: Post
    ok: bool
    attachment: Attachment | None = None
    path: str | None = None
    sha256: str | None = None
    size: int | None = None
    error: str | None = None
    other_attachments: list[str] = field(default_factory=list)
    fetched_at: str = field(default_factory=utc_now_iso)

    def to_json(self) -> dict[str, Any]:
        return {
            "ntt": self.post.ntt,
            "title": self.post.title,
            "registered": self.post.registered,
            "post_url": self.post.url,
            "ok": self.ok,
            "file_name": self.attachment.name if self.attachment else None,
            "format": self.attachment.ext if self.attachment else None,
            "download_url": self.attachment.url if self.attachment else None,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "error": self.error,
            "other_attachments": self.other_attachments,
            "fetched_at": self.fetched_at,
        }


def parse_list(page_html: str) -> list[Post]:
    doc = lhtml.fromstring(page_html)
    posts: list[Post] = []
    for tr in doc.iter("tr"):
        a = next((x for x in tr.iter("a") if "selectBbsNttView.do" in (x.get("href") or "")), None)
        if a is None:
            continue
        m = re.search(r"nttSn=(\d+)", a.get("href") or "")
        if not m:
            continue
        title = " ".join(a.text_content().split())
        reg = None
        for td in tr.iter("td"):
            t = td.text_content().strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
                reg = t
        posts.append(Post(m.group(1), title, reg, VIEW_URL.format(ntt=m.group(1))))
    return posts


def parse_attachments(view_html: str) -> list[Attachment]:
    doc = lhtml.fromstring(view_html)
    out: list[Attachment] = []
    for a in doc.iter("a"):
        href = a.get("href") or ""
        m = re.search(r"downloadBbsFile\.do\?atchmnflNo=(\d+)", href)
        if not m or "p-attach__link" not in (a.get("class") or ""):
            continue
        size_el = next(
            (s for s in a.iter("span") if "p-attach__size" in (s.get("class") or "")), None
        )
        size_text = " ".join(size_el.text_content().split()) if size_el is not None else ""
        full = " ".join(a.text_content().split())
        name = full.replace(size_text, "").strip()
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        out.append(
            Attachment(
                m.group(1),
                name,
                ext,
                size_text,
                BASE + f"downloadBbsFile.do?atchmnflNo={m.group(1)}",
            )
        )
    return out


def list_posts(client: httpx.Client, max_pages: int = 10) -> list[Post]:
    seen: dict[str, Post] = {}
    for page in range(1, max_pages + 1):
        r = client.get(LIST_URL.format(page=page))
        r.raise_for_status()
        posts = parse_list(r.text)
        new = [p for p in posts if p.ntt not in seen]
        if not new:
            break
        for p in new:
            seen[p.ntt] = p
    return list(seen.values())


def _filename(resp: httpx.Response, fallback: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    return unquote(m.group(1)).strip() if m else fallback


def manifest_path() -> Path:
    return manifests_dir() / "contract.json"


def load_manifest() -> dict[str, Any] | None:
    p = manifest_path()
    if not p.exists():
        return None
    data: dict[str, Any] = read_json(p)
    return data


def fetch_contracts(
    *,
    client: httpx.Client | None = None,
    title_filter: re.Pattern[str] = TITLE_RE,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    own = client is None
    cl = client or httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=60.0, follow_redirects=True
    )
    out_dir = raw_dir("contract")
    results: list[FetchedContract] = []
    board_error: str | None = None
    try:
        try:
            posts = [p for p in list_posts(cl) if title_filter.search(p.title)]
        except httpx.HTTPError as e:
            posts = []
            board_error = f"{type(e).__name__}: {e}"
        for post in posts:
            fc = FetchedContract(post, ok=False)
            try:
                r = cl.get(post.url)
                r.raise_for_status()
                atts = parse_attachments(r.text)
                if not atts:
                    raise RuntimeError("no attachment on the post")
                atts.sort(key=lambda a: (FORMAT_RANK.get(a.ext, 9), a.file_no))
                best = atts[0]
                fc.attachment = best
                fc.other_attachments = [a.name for a in atts[1:]]
                d = cl.get(best.url)
                d.raise_for_status()
                data = d.content
                if not data:
                    raise RuntimeError("empty download")
                fname = _filename(d, best.name)
                ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else best.ext
                dest = out_dir / f"{best.file_no}.{ext}"
                dest.write_bytes(data)
                fc.path = f"data/raw/contract/{dest.name}"
                fc.sha256 = sha256_bytes(data)
                fc.size = len(data)
                fc.ok = True
                log(f"ok  {post.ntt} {post.title} -> {dest.name} ({len(data)} bytes)")
            except (httpx.HTTPError, RuntimeError) as e:
                fc.error = f"{type(e).__name__}: {e}"
                log(f"ERR {post.ntt} {post.title}: {fc.error}")
            results.append(fc)
    finally:
        if own:
            cl.close()
    m = {
        "source": "ftc_std_contract",
        "board": LIST_URL.format(page=1),
        "license_note": BOARD_NOTE,
        "fetched_at": utc_now_iso(),
        "title_filter": title_filter.pattern,
        "board_error": board_error,
        "documents": [fc.to_json() for fc in results],
        "failures": [fc.post.ntt for fc in results if not fc.ok],
    }
    write_json(manifest_path(), m)
    return m
