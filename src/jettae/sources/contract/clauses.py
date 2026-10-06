"""Rule-based extraction of payment-term (지급기한) and start-date (기산점) clauses with spans.

A paragraph is a candidate when it mentions payment (지급/지불) of a price (대금) and states a
start date ("상품수령일로부터", "월 판매마감일부터", ...) and/or a period ("(  )일 이내",
"60일 이내"). Blank periods in standard forms ("(  )일") are reported as ``term_blank`` —
never filled in. Paragraphs starting with "※" are the FTC's drafting notes and are kept
separate (``kind="drafting_note"``) from contract text (``kind="clause"``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jettae.sources.contract.hwp5 import Paragraph

ART_RE = re.compile(
    r"^\s*제\s*(\d+)\s*조(?:\s*의\s*(\d+))?\s*[\[(（【〔<]\s*([^\])）】〕>]{1,60})[\])）】〕>]"
)
PAY_RE = re.compile(r"지급|지불")
PRICE_RE = re.compile(r"대금")
BASE_RE = re.compile(
    r"(?P<base>상품\s*수령일|(?:상품\s*)?입고일|월\s*판매\s*마감일|판매\s*마감일|매입\s*마감일|"
    r"납품일|수령일|검수\s*완료일|마감일)\s*(?:\([^()]{0,80}\))?\s*(?:로|으로)?\s*부터"
)
TERM_RE = re.compile(
    r"(?P<val>\d+|\(\s*\)|（\s*）|○+|□+|_{2,})\s*일\s*(?P<suffix>이내|안에|내에|까지)"
)
BASE_KIND = {
    "상품수령일": "goods_received_date",
    "상품입고일": "goods_in_date",  # 입고일: wording differs from the statute's 상품수령일
    "입고일": "goods_in_date",
    "월판매마감일": "sales_close_date",
    "판매마감일": "sales_close_date",
    "매입마감일": "purchase_close_date",
    "납품일": "delivery_date",
    "수령일": "received_date",
    "검수완료일": "inspection_date",
    "마감일": "close_date",
}


@dataclass(frozen=True)
class Clause:
    kind: str  # "clause" | "drafting_note"
    article_no: str | None
    article_title: str | None
    text: str
    locator: dict[str, int]  # section, paragraph, level, char_start, char_end (doc offsets)
    base_kind: str | None = None
    base_text: str | None = None
    base_span: tuple[int, int] | None = None  # within ``text``
    term_days: int | None = None
    term_blank: bool = False
    term_text: str | None = None
    term_span: tuple[int, int] | None = None

    def to_json(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["base_span"] = list(self.base_span) if self.base_span else None
        d["term_span"] = list(self.term_span) if self.term_span else None
        return d


@dataclass
class Extraction:
    paragraphs: int
    articles: list[tuple[str, str]] = field(default_factory=list)
    clauses: list[Clause] = field(default_factory=list)
    text: str = ""

    def payment_clauses(self) -> list[Clause]:
        return [c for c in self.clauses if c.kind == "clause"]

    def notes(self) -> list[Clause]:
        return [c for c in self.clauses if c.kind == "drafting_note"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def extract(paragraphs: list[Paragraph]) -> Extraction:
    """Extract payment-term / start-date clauses. ``Extraction.text`` is the document text
    (paragraphs joined by newlines) that ``locator.char_start/char_end`` index into."""
    ex = Extraction(len(paragraphs))
    parts: list[str] = []
    offset = 0
    art_no: str | None = None
    art_title: str | None = None
    for p in paragraphs:
        text = p.text
        start = offset
        parts.append(text)
        offset += len(text) + 1
        m = ART_RE.match(text)
        if m and p.level == 0:
            art_no = m.group(1) + (f"의{m.group(2)}" if m.group(2) else "")
            art_title = m.group(3).strip()
            ex.articles.append((art_no, art_title))
        if not (PAY_RE.search(text) and PRICE_RE.search(text)):
            continue
        bm = BASE_RE.search(text)
        tm = TERM_RE.search(text, bm.end() if bm else 0) or TERM_RE.search(text)
        if bm is None and tm is None:
            continue
        kind = "drafting_note" if text.lstrip().startswith("※") else "clause"
        term_days: int | None = None
        blank = False
        if tm is not None:
            v = tm.group("val")
            if v.isdigit():
                term_days = int(v)
            else:
                blank = True
        ex.clauses.append(
            Clause(
                kind=kind,
                article_no=art_no,
                article_title=art_title,
                text=text,
                locator={
                    "section": p.section,
                    "paragraph": p.index,
                    "level": p.level,
                    "char_start": start,
                    "char_end": start + len(text),
                },
                base_kind=BASE_KIND.get(_norm(bm.group("base"))) if bm else None,
                base_text=bm.group(0) if bm else None,
                base_span=bm.span() if bm else None,
                term_days=term_days,
                term_blank=blank,
                term_text=tm.group(0) if tm else None,
                term_span=tm.span() if tm else None,
            )
        )
    ex.text = "\n".join(parts)
    return ex


def contract_type(title: str) -> str | None:
    """Trade type implied by a standard-contract title (직매입 / 특약매입·위수탁)."""
    t = _norm(title)
    if "직매입" in t:
        return "direct"
    if "특약매입" in t or "위수탁" in t:
        return "consignment"
    return None
