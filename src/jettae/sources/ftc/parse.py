"""Parse a DRF ``FtcService`` decision XML into sections, footnotes and table references.

Offsets: every position is a character offset into the *section text* exactly as it appears in
the XML element (CDATA content, unmodified), so provenance is ``(decision_id, section,
char_start, char_end)`` and ``sections[section][char_start:char_end]`` reproduces the excerpt.

Two footnote layouts occur in real decisions:

- older: ``<각주>N</각주>`` markers inside the text + a ``<각주목록>`` list of
  ``<각주번호>``/``<각주내용>`` pairs (section name ``각주:N``);
- newer: the footnote text is spliced inline into the paragraph, surrounded by a blank line
  before and two blank lines after. Detected heuristically (``Footnote.inline=True``,
  ``confidence="low"``).

Tables are images (``<img src="/LSW/flDownload.do?flSeq=...">``). A caption line
``<표 N>   제목`` is attached to the image directly after it (allowing a unit line such as
``(단위: 원, VAT 포함)``), otherwise to the image directly before it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from lxml import etree

# text-bearing sections, in document order
SECTION_TAGS = (
    "피심정보내용",
    "심의정보내용",
    "주문",
    "신청취지",
    "이유",
    "별지",
    "결정요지",
)

_IMG_RE = re.compile(r'<img\s+src="(?P<src>[^"]*)"(?:\s+alt="(?P<alt>[^"]*)")?\s*>\s*(?:</img>)?')
_FLSEQ_RE = re.compile(r"flSeq=(\d+)")
# caption: "<표 11>   제목" or "<표 9>" alone at line end; not inline references like "<표 5>와"
_CAPTION_RE = re.compile(
    r"<(?P<kind>표|별지)\s*(?P<no>\d+)>(?:[ \t　]{2,}(?P<title>[^\n<]*)|[ \t]*(?=\n|$))"
)
_BRACKET_CAPTION_RE = re.compile(r"^\[(?P<kind>별지)\s*(?P<no>\d*)\]\s*(?P<title>[^\n<]*)", re.M)
# unit line: "(단위: 원, VAT 포함)" or "[단위: 개, 건, 일, 원(부가가치세 포함)]"
_UNIT_RE = re.compile(
    r"\((?P<p>[^()\n]*단위\s*[:：][^()\n]*)\)|\[(?P<b>[^\[\]\n]*단위\s*[:：][^\[\]\n]*)\]"
)
_FN_MARK_RE = re.compile(r"<각주>\s*(\d+)\s*</각주>")
# inline footnote text: one line, may mention tables ("<표 11>") but holds no image tag
_INLINE_FN_RE = re.compile(
    r"(?<=[^\n])\n\n(?P<fn>(?:[^\n<]|<(?:표|별지)\s*\d+>){2,800})\n\n\n(?=[^\n])"
)
_DATE_RE = re.compile(r"(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})")

DELAY_TABLE_KEYWORDS = ("지연", "이자", "지급", "공탁")


@dataclass(frozen=True)
class Footnote:
    no: int | None
    text: str
    section: str  # where ``text`` lives: "각주:N" (list) or the host section (inline)
    char_start: int
    char_end: int
    inline: bool = False
    marker_section: str | None = None  # section of the ``<각주>N</각주>`` marker
    marker_pos: int | None = None
    confidence: str = "high"


@dataclass(frozen=True)
class TableImage:
    section: str
    index: int  # 0-based order of the image inside the decision
    src: str
    flseq: str | None  # None for unlinked images (e.g. "table_image_14(1).png")
    alt: str
    char_start: int
    char_end: int
    label: str | None = None  # "표 11" / "별지 2"
    caption: str | None = None
    caption_pos: int | None = None
    unit_note: str | None = None
    context_before: str = ""
    context_after: str = ""
    continuation: bool = False  # image directly continues the previous table

    @property
    def number(self) -> int | None:
        if self.label and (m := re.search(r"(\d+)", self.label)):
            return int(m.group(1))
        return None


@dataclass
class FtcDecision:
    decision_id: str
    case_no: str
    title: str
    decision_no: str
    decision_date: date | None
    doc_type: str
    meeting: str
    sections: dict[str, str]
    footnotes: list[Footnote] = field(default_factory=list)
    tables: list[TableImage] = field(default_factory=list)

    def text(self, section: str) -> str:
        return self.sections.get(section, "")

    def excerpt(self, section: str, start: int, end: int) -> str:
        return self.sections.get(section, "")[start:end]

    def all_sections(self) -> list[tuple[str, str]]:
        return list(self.sections.items())


def parse_kdate(value: str) -> date | None:
    """``"2026.2.10."`` / ``"2026. 2. 10."`` -> date; None when not parseable."""
    m = _DATE_RE.search(value or "")
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _text(el: etree._Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext())


def parse_decision(data: bytes | str) -> FtcDecision:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    root = etree.fromstring(raw, parser=parser)
    if root.tag != "FtcService":
        raise ValueError(f"not an FtcService document (root <{root.tag}>)")

    def field_(tag: str) -> str:
        return re.sub(r"\s+", " ", _text(root.find(tag))).strip()

    sections: dict[str, str] = {}
    for tag in SECTION_TAGS:
        el = root.find(f".//{tag}")
        txt = _text(el)
        if txt.strip():
            sections[tag] = txt

    footnotes: list[Footnote] = []
    fl = root.find("각주목록")
    if fl is not None:
        current: int | None = None
        for child in fl:
            if child.tag == "각주번호":
                try:
                    current = int(_text(child).strip())
                except ValueError:
                    current = None
            elif child.tag == "각주내용":
                txt = _text(child)
                key = f"각주:{current}" if current is not None else f"각주:?{len(footnotes)}"
                sections[key] = txt
                footnotes.append(Footnote(current, txt.strip(), key, 0, len(txt)))

    dec = FtcDecision(
        decision_id=field_("결정문일련번호"),
        case_no=field_("사건번호"),
        title=field_("사건명"),
        decision_no=field_("결정번호"),
        decision_date=parse_kdate(field_("결정일자")),
        doc_type=field_("문서유형"),
        meeting=field_("회의종류"),
        sections=sections,
    )
    dec.footnotes = _link_markers(footnotes, sections) + _inline_footnotes(sections)
    dec.tables = _tables(sections)
    return dec


def _link_markers(footnotes: list[Footnote], sections: dict[str, str]) -> list[Footnote]:
    pos: dict[int, tuple[str, int]] = {}
    for name in SECTION_TAGS:
        for m in _FN_MARK_RE.finditer(sections.get(name, "")):
            pos.setdefault(int(m.group(1)), (name, m.start()))
    out = []
    for f in footnotes:
        if f.no is not None and f.no in pos:
            sec, p = pos[f.no]
            out.append(Footnote(f.no, f.text, f.section, f.char_start, f.char_end, False, sec, p))
        else:
            out.append(f)
    return out


def _inline_footnotes(sections: dict[str, str]) -> list[Footnote]:
    out: list[Footnote] = []
    for name in ("이유", "주문", "별지"):
        txt = sections.get(name, "")
        for m in _INLINE_FN_RE.finditer(txt):
            fn = m.group("fn")
            # numbered paragraphs ("33   피심인은...") and headings are not footnotes
            if re.match(r"\s*(\d{1,3}\s{2,}|[가-하]\.\s|\d\)\s|[가-하]\)\s|\[)", fn):
                continue
            out.append(
                Footnote(
                    None,
                    fn.strip(),
                    name,
                    m.start("fn"),
                    m.end("fn"),
                    inline=True,
                    marker_section=name,
                    marker_pos=m.start(),
                    confidence="low",
                )
            )
    return out


def _captions(txt: str) -> list[tuple[int, int, str, str]]:
    """(start, end, label, title) of caption lines in ``txt``."""
    caps = []
    for m in _CAPTION_RE.finditer(txt):
        title = (m.group("title") or "").strip()
        caps.append((m.start(), m.end(), f"{m.group('kind')} {m.group('no')}", title))
    for m in _BRACKET_CAPTION_RE.finditer(txt):
        label = f"별지 {m.group('no')}".strip()
        caps.append((m.start(), m.end(), label, (m.group("title") or "").strip()))
    caps.sort()
    return caps


def _only_unit_lines(between: str) -> bool:
    lines = [ln.strip() for ln in between.split("\n") if ln.strip()]
    return len(lines) <= 2 and all(_UNIT_RE.search(ln) or ln.startswith(("(", "[")) for ln in lines)


def _first_unit(segment: str) -> str | None:
    m = _UNIT_RE.search(segment)
    return (m.group("p") or m.group("b")).strip() if m else None


def _tables(sections: dict[str, str]) -> list[TableImage]:
    out: list[TableImage] = []
    idx = 0
    for name in SECTION_TAGS:
        txt = sections.get(name, "")
        imgs = list(_IMG_RE.finditer(txt))
        if not imgs:
            continue
        caps = _captions(txt)
        assigned: dict[int, tuple[int, int, str, str]] = {}  # img -> (start, end, label, title)
        used: set[int] = set()
        # pass 1: caption directly before the image (only a unit line / blank lines between)
        for ci, cap in enumerate(caps):
            nxt = next((i for i, m in enumerate(imgs) if m.start() >= cap[1]), None)
            if nxt is None or nxt in assigned:
                continue
            if _only_unit_lines(txt[cap[1] : imgs[nxt].start()]):
                assigned[nxt] = cap
                used.add(ci)
        # pass 2: caption directly after an image that has none yet
        for ci, cap in enumerate(caps):
            if ci in used:
                continue
            prv = next((i for i in range(len(imgs) - 1, -1, -1) if imgs[i].end() <= cap[0]), None)
            if prv is None or prv in assigned:
                continue
            if not txt[imgs[prv].end() : cap[0]].strip():
                assigned[prv] = cap
                used.add(ci)
        prev: TableImage | None = None
        for i, m in enumerate(imgs):
            src = m.group("src")
            fm = _FLSEQ_RE.search(src)
            icap = assigned.get(i)
            label = icap[2] if icap else None
            title = icap[3] if icap else None
            unit: str | None = None
            if icap and icap[0] < m.start():
                unit = _first_unit(txt[icap[1] : m.start()])
            if unit is None:
                # unit line directly above the image (no caption) or directly below it
                above = txt[: m.start()].rstrip("\n").rsplit("\n", 1)[-1]
                unit = _first_unit(above) if _UNIT_RE.search(above) else None
            if unit is None:
                nxt_start = imgs[i + 1].start() if i + 1 < len(imgs) else len(txt)
                below = txt[m.end() : nxt_start].lstrip("\n").split("\n", 1)[0]
                unit = _first_unit(below)
            continuation = False
            if (
                icap is None
                and prev is not None
                and prev.section == name
                and not txt[prev.char_end : m.start()].strip()
            ):
                # an image directly following another image continues that table
                label, title, continuation = prev.label, prev.caption, True
                unit = unit or prev.unit_note
            lo = max(0, (icap[0] if icap and icap[0] < m.start() else m.start()) - 400)
            hi = min(len(txt), m.end() + 300)
            t = TableImage(
                section=name,
                index=idx,
                src=src,
                flseq=fm.group(1) if fm else None,
                alt=m.group("alt") or "",
                char_start=m.start(),
                char_end=m.end(),
                label=label,
                caption=title,
                caption_pos=icap[0]
                if icap
                else (prev.caption_pos if continuation and prev else None),
                unit_note=unit,
                context_before=_IMG_RE.sub("[img]", txt[lo : m.start()]),
                context_after=_IMG_RE.sub("[img]", txt[m.end() : hi]),
                continuation=continuation,
            )
            out.append(t)
            prev = t
            idx += 1
    return out


def unit_multiplier(unit_note: str | None) -> int:
    """KRW multiplier implied by a unit note: 천 원 -> 1000, 백만 원 -> 1_000_000, else 1."""
    if not unit_note:
        return 1
    m = re.search(r"단위\s*[:：]\s*([^,)]*)", unit_note)
    unit = re.sub(r"\s+", "", m.group(1) if m else unit_note)
    if unit.startswith("백만원"):
        return 1_000_000
    if unit.startswith("천원"):
        return 1000
    return 1


def delay_table_score(t: TableImage) -> tuple[int, str]:
    """How likely ``t`` is a payment-delay / delay-interest table, with a reason.

    2 = caption names 지연/이자 (or 지급+대금/공탁), 1 = no caption but the text right before
    the image talks about 지연이자, 0 = otherwise.
    """
    cap = t.caption or ""
    if cap:
        if re.search(r"서면|계약|발급|교부|파견", cap) and not re.search(r"대금|이자", cap):
            return 0, f"caption: {cap}"
        if re.search(r"지연|이자", cap) and re.search(r"지급|이자|대금|공탁|내역", cap):
            return 2, f"caption: {cap}"
        if re.search(r"공탁", cap) or (re.search(r"지급", cap) and re.search(r"대금", cap)):
            return 2, f"caption: {cap}"
        if re.search(r"계약|서면|파견|반품|과징금|현황|점유율|일반", cap):
            return 0, f"caption: {cap}"
    near = t.context_before[-300:]
    if not cap and re.search(r"지연\s*이자|지연\s*지급|법정\s*지급\s*기한", near):
        return 1, "uncaptioned image after text on 지연이자/법정지급기한"
    if t.section == "별지" and re.search(r"지연|이자", t.caption or t.context_before[-200:]):
        return 1, "별지 near 지연/이자"
    return 0, f"caption: {cap}" if cap else "no caption"
