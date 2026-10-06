"""법제처 DRF Open API client for statute articles (target=law) and notices (target=admrul).

The OC key comes from ``JETTAE_DRF_OC`` (default: the public sample key ``test``). Documents
are found by *search* (exact name, 현행), not by hard-coded serial numbers, so a new
amendment is picked up. Raw XML goes to ``data/raw/law/``; the manifest keeps URLs (personal
OC redacted), hashes, effective dates and the extracted paragraph texts (Korean statutes and
notices are not protected works under 저작권법 제7조).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from lxml import etree

from jettae.sources import (
    USER_AGENT,
    manifests_dir,
    raw_dir,
    read_json,
    redact_url,
    sha256_bytes,
    utc_now_iso,
    write_json,
)

SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"
LICENSE = "대한민국 법령·행정규칙 원문(저작권법 제7조: 보호받지 못하는 저작물)"


def drf_oc() -> str:
    return os.environ.get("JETTAE_DRF_OC", "").strip() or "test"


class DrfError(RuntimeError):
    pass


@dataclass(frozen=True)
class DocSpec:
    key: str
    target: str  # "law" | "admrul"
    name: str
    article: int | None = None  # for target=law
    required: bool = True


SPECS: tuple[DocSpec, ...] = (
    DocSpec("large_retail_art8", "law", "대규모유통업에서의 거래 공정화에 관한 법률", 8),
    DocSpec("large_retail_interest", "admrul", "상품판매대금 등 지연지급 시의 지연이율 고시"),
    DocSpec("subcontract_art13", "law", "하도급거래 공정화에 관한 법률", 13, required=False),
    DocSpec(
        "subcontract_interest", "admrul", "선급금 등 지연지급 시의 지연이율 고시", required=False
    ),
)


@dataclass
class FetchedDoc:
    key: str
    target: str
    name: str
    ok: bool
    url: str = ""
    search_url: str = ""
    serial: str | None = None  # 법령일련번호(MST) / 행정규칙일련번호(ID)
    effective_date: str | None = None  # 시행일자 YYYY-MM-DD
    issued: str | None = None  # 공포일자 / 발령일자
    number: str | None = None  # 공포번호 / 발령번호
    article: int | None = None
    article_title: str | None = None
    paragraphs: list[str] = field(default_factory=list)
    sha256: str | None = None
    raw_path: str | None = None
    error: str | None = None
    fetched_at: str = field(default_factory=utc_now_iso)

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _iso(yyyymmdd: str | None) -> str | None:
    if not yyyymmdd or len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return None
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _text(el: etree._Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _parse(content: bytes, url: str) -> etree._Element:
    try:
        root = etree.fromstring(content)
    except etree.XMLSyntaxError as e:
        raise DrfError(f"{redact_url(url)}: response is not XML ({e})") from e
    if root.tag == "Response":
        raise DrfError(
            f"DRF rejected the request: {_text(root.find('result'))} {_text(root.find('msg'))}"
        )
    return root


def _url(base: str, params: dict[str, str]) -> str:
    return f"{base}?{urlencode(params)}"


def search(client: httpx.Client, spec: DocSpec, oc: str) -> tuple[dict[str, str], str]:
    """Exact-name, 현행 search hit (as a flat dict of child texts) and the search URL."""
    params = {"OC": oc, "target": spec.target, "type": "XML", "query": spec.name}
    url = _url(SEARCH_URL, params)
    r = client.get(url)
    r.raise_for_status()
    root = _parse(r.content, url)
    item_tag, name_tag, state_tag = (
        ("law", "법령명한글", "현행연혁코드")
        if spec.target == "law"
        else ("admrul", "행정규칙명", "현행연혁구분")
    )
    hits = []
    for it in root.iter(item_tag):
        d = {c.tag: _text(c) for c in it if isinstance(c.tag, str)}
        if d.get(name_tag, "").replace(" ", "") == spec.name.replace(" ", ""):
            hits.append(d)
    current = [h for h in hits if h.get(state_tag) == "현행"] or hits
    if not current:
        raise DrfError(f"no exact search hit for {spec.name!r} ({spec.target})")
    current.sort(key=lambda h: h.get("시행일자", ""), reverse=True)
    return current[0], url


def _law_article(root: etree._Element, article: int) -> tuple[str, list[str]]:
    for unit in root.iter("조문단위"):
        if _text(unit.find("조문번호")) != str(article) or _text(unit.find("조문여부")) != "조문":
            continue
        if _text(unit.find("조문가지번호")):
            continue
        paras: list[str] = []
        head = _text(unit.find("조문내용"))
        if head:
            paras.append(head)
        for hang in unit.findall("항"):
            t = _text(hang.find("항내용"))
            if t:
                paras.append(t)
            for ho in hang.findall("호"):
                t = _text(ho.find("호내용"))
                if t:
                    paras.append(t)
                for mok in ho.findall("목"):
                    t = _text(mok.find("목내용"))
                    if t:
                        paras.append(t)
        return _text(unit.find("조문제목")), paras
    raise DrfError(f"article {article} not found in response")


def _admrul_paragraphs(root: etree._Element) -> list[str]:
    paras: list[str] = []
    for el in root.iter("조문내용"):
        for line in _text(el).splitlines():
            line = line.strip().strip("　").strip()
            if line:
                paras.append(line)
    for el in root.iter("부칙내용"):
        t = " ".join(x.strip() for x in _text(el).splitlines() if x.strip())
        if t:
            paras.append(t)
    return paras


def fetch_doc(client: httpx.Client, spec: DocSpec, oc: str, out_dir: Path) -> FetchedDoc:
    doc = FetchedDoc(spec.key, spec.target, spec.name, ok=False, article=spec.article)
    try:
        hit, surl = search(client, spec, oc)
        doc.search_url = redact_url(surl)
        if spec.target == "law":
            doc.serial = hit.get("법령일련번호")
            doc.effective_date = _iso(hit.get("시행일자"))
            doc.issued = _iso(hit.get("공포일자"))
            doc.number = hit.get("공포번호")
            if spec.article is None:
                raise DrfError("law spec without article")
            params = {
                "OC": oc,
                "target": "law",
                "MST": doc.serial or "",
                "type": "XML",
                "JO": f"{spec.article:04d}00",
            }
        else:
            doc.serial = hit.get("행정규칙일련번호")
            doc.effective_date = _iso(hit.get("시행일자"))
            doc.issued = _iso(hit.get("발령일자"))
            doc.number = hit.get("발령번호")
            params = {"OC": oc, "target": "admrul", "ID": doc.serial or "", "type": "XML"}
        url = _url(SERVICE_URL, params)
        doc.url = redact_url(url)
        r = client.get(url)
        r.raise_for_status()
        root = _parse(r.content, url)
        if spec.target == "law":
            doc.article_title, doc.paragraphs = _law_article(root, spec.article or 0)
        else:
            doc.paragraphs = _admrul_paragraphs(root)
            doc.effective_date = _iso(_text(root.find(".//시행일자"))) or doc.effective_date
        if not doc.paragraphs:
            raise DrfError("no text paragraphs in response")
        doc.sha256 = sha256_bytes(r.content)
        raw = out_dir / f"{spec.key}.xml"
        raw.write_bytes(r.content)
        doc.raw_path = f"data/raw/law/{raw.name}"
        doc.ok = True
    except (httpx.HTTPError, DrfError) as e:
        doc.error = f"{type(e).__name__}: {redact_url(str(e))}"
    return doc


def manifest_path() -> Path:
    return manifests_dir() / "law.json"


def load_manifest() -> dict[str, Any] | None:
    p = manifest_path()
    if not p.exists():
        return None
    data: dict[str, Any] = read_json(p)
    return data


def fetch_all(
    *,
    client: httpx.Client | None = None,
    specs: tuple[DocSpec, ...] = SPECS,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    oc = drf_oc()
    own = client is None
    cl = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0)
    out_dir = raw_dir("law")
    try:
        docs = []
        for spec in specs:
            d = fetch_doc(cl, spec, oc, out_dir)
            log(
                f"{'ok ' if d.ok else 'ERR'} {spec.key}: {spec.name}"
                + (f" (시행 {d.effective_date}, {len(d.paragraphs)} paragraphs)" if d.ok else "")
                + (f" — {d.error}" if d.error else "")
            )
            docs.append(d)
    finally:
        if own:
            cl.close()
    m = {
        "source": "law",
        "api": "법제처 DRF Open API (lawSearch.do / lawService.do)",
        "oc": "test (public sample key)"
        if oc == "test"
        else "personal key from JETTAE_DRF_OC (redacted)",
        "license": LICENSE,
        "fetched_at": utc_now_iso(),
        "documents": {d.key: d.to_json() for d in docs},
        "failures": [d.key for d in docs if not d.ok],
    }
    write_json(manifest_path(), m)
    return m
