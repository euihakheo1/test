"""Case-level facts from decision text (regex first), with provenance.

Every :class:`CaseFact` carries ``(decision_id, section, char_start, char_end)`` such that
``decision.sections[section][char_start:char_end] == raw``. Facts are extracted only from
"delay paragraphs" (a numbered paragraph of 이유 that mentions 지연 together with 대금/이자/기한)
plus footnotes; they are *claims of the decision text*, not computations.

Masking: decisions mask business numbers with ``*`` (e.g. ``**개``, ``*,***,***원``). Such
matches are recorded with ``masked=True`` and ``value=None`` — never guessed.

Confidence: ``high`` for specific phrasings (``지연이자 총 N원``, ``최소 N일에서 최대 M일``,
``지연이율은 연 N%``), ``low`` for generic matches whose role is inferred from nearby words.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from jettae.domain.models import SourceSpan
from jettae.sources.ftc.parse import FtcDecision

EXTRACTOR = "ftc.regex.v1"

_NUM = r"[\d*][\d*,]*"
_PARA_START = re.compile(
    r"^(?:\d{1,3}[ \t]+\S|[가-하]\.\s|\d\)\s|[가-하]\)\s|<표\s*\d+>|\[별지)", re.M
)
_TABLE_REF = re.compile(r"<(표|별지)\s*(\d+)>")
_AMOUNT = re.compile(rf"(?P<num>{_NUM})\s*(?P<unit>백만|천)?\s*원")
_COUNT_SUPPLIER = re.compile(
    rf"(?P<num>{_NUM})\s*개\s*(?:의\s*)?(?P<who>납품업자|수급사업자|매장임차인|납품업체|협력업체|사업자)"
)
_COUNT_TXN = re.compile(rf"(?:총\s*)?(?P<num>{_NUM})\s*건")
_DAYS_MINMAX = re.compile(
    rf"최소\s*(?P<lo>{_NUM})\s*일\s*(?:에서|부터|～|~|∼)\s*최대\s*(?P<hi>{_NUM})\s*일"
)
_DAYS_RANGE = re.compile(rf"(?P<lo>{_NUM})\s*일?\s*[~～∼]\s*(?P<hi>{_NUM})\s*일")
_DAYS_MAX = re.compile(rf"최대\s*(?P<hi>{_NUM})\s*일")
_RATE = re.compile(r"연\s*(?:리\s*)?(?:100분의\s*)?(?P<num>\d+(?:\.\d+)?)\s*(?P<pct>%|퍼센트)")
_TERM = re.compile(
    r"(?P<base>상품수령일|상품\s*하차일|월\s*판매\s*마감일|판매마감일|목적물(?:\s*등의)?\s*수령일|"
    r"매입기간\s*종료일|매입마감일|검수일|입고일)\s*(?:로)?부터\s*(?P<days>\d+)\s*일"
)
_BASE_DEF = re.compile(
    r"상품수령일|상품\s*하차일|하차일|판매\s*마감일|목적물(?:\s*등의)?\s*수령일|매입기간\s*종료일|"
    r"매입마감일|기산일|입고일|검수일|수령한\s*날|인도한\s*날"
)
_ROUNDING = re.compile(r"절사|반올림|원\s*단위\s*미만|원\s*미만|버림|사사오입")
_HOLIDAY = re.compile(r"공휴일|토요일|일요일|휴일|익일|다음\s*날|영업일")
_SENT_END = re.compile(r"(?<=다\.)\s|(?<=다\.)$|\n\n")


@dataclass(frozen=True)
class CaseFact:
    decision_id: str
    kind: str
    value: Any  # int | str | dict | None (masked)
    raw: str
    section: str
    char_start: int
    char_end: int
    confidence: str = "high"
    masked: bool = False
    in_footnote: bool = False
    context: str = ""
    table_refs: tuple[str, ...] = ()
    unit_multiplier: int = 1
    note: str = ""
    extractor: str = EXTRACTOR

    def span(self, doc_version_id: str) -> SourceSpan:
        return SourceSpan(
            doc_version_id=doc_version_id,
            locator={
                "decision_id": self.decision_id,
                "section": self.section,
                "char_start": self.char_start,
                "char_end": self.char_end,
            },
            excerpt=self.raw,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "kind": self.kind,
            "value": self.value,
            "raw": self.raw,
            "section": self.section,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "confidence": self.confidence,
            "masked": self.masked,
            "in_footnote": self.in_footnote,
            "context": self.context,
            "table_refs": list(self.table_refs),
            "unit_multiplier": self.unit_multiplier,
            "note": self.note,
            "extractor": self.extractor,
        }


@dataclass
class Paragraph:
    section: str
    start: int
    end: int
    text: str
    footnote_spans: list[tuple[int, int]] = field(default_factory=list)

    @property
    def table_refs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(f"{k} {n}" for k, n in _TABLE_REF.findall(self.text)))


def parse_number(raw: str) -> int | None:
    """``"1,234"`` -> 1234; a masked number (any ``*``) -> None."""
    if "*" in raw:
        return None
    digits = raw.replace(",", "")
    return int(digits) if digits.isdigit() else None


def paragraphs(dec: FtcDecision, section: str = "이유") -> list[Paragraph]:
    txt = dec.text(section)
    if not txt:
        return []
    starts = sorted({0, *(m.start() for m in _PARA_START.finditer(txt))})
    bounds = list(zip(starts, [*starts[1:], len(txt)], strict=True))
    fns = [(f.char_start, f.char_end) for f in dec.footnotes if f.inline and f.section == section]
    out = []
    for s, e in bounds:
        if not txt[s:e].strip():
            continue
        out.append(
            Paragraph(section, s, e, txt[s:e], [(a, b) for a, b in fns if s <= a and b <= e])
        )
    return out


def is_delay_paragraph(text: str) -> bool:
    return bool(re.search(r"지연", text)) and bool(re.search(r"대금|이자|기한|지급", text))


def _sentence(text: str, pos: int, end: int) -> tuple[int, int]:
    """Bounds of the sentence around [pos, end) inside ``text`` (relative offsets)."""
    s = 0
    for m in _SENT_END.finditer(text, 0, pos):
        s = m.end()
    m2 = _SENT_END.search(text, end)
    e = m2.start() if m2 else len(text)
    return s, e


def _before(text: str, pos: int, n: int = 40) -> str:
    return re.sub(r"\s+", " ", text[max(0, pos - n) : pos])


def _after(text: str, pos: int, n: int = 30) -> str:
    return re.sub(r"\s+", " ", text[pos : pos + n])


def _near_refs(text: str, pos: int, window: int = 80) -> tuple[str, ...]:
    """The last ``<표 N>`` mentioned within ``window`` chars before ``pos`` (else empty)."""
    refs = _TABLE_REF.findall(text[max(0, pos - window) : pos])
    return (f"{refs[-1][0]} {refs[-1][1]}",) if refs else ()


def _skip_footnote(rel_pos: int, fn_spans: list[tuple[int, int]], offset: int) -> int:
    """If an inline footnote starts right after ``rel_pos``, return the position after it."""
    for a, b in fn_spans:
        if 0 <= (a - offset) - rel_pos <= 3:
            return b - offset
    return rel_pos


def extract_case_facts(dec: FtcDecision) -> list[CaseFact]:
    facts: list[CaseFact] = []
    seen: set[tuple[str, str, int, int]] = set()

    def add(f: CaseFact) -> None:
        key = (f.kind, f.section, f.char_start, f.char_end)
        if key not in seen:
            seen.add(key)
            facts.append(f)

    for para in paragraphs(dec, "이유"):
        if not is_delay_paragraph(para.text):
            continue
        for f in _facts_in_block(
            dec,
            para.section,
            para.start,
            para.text,
            para.footnote_spans,
            para.table_refs,
            footnote=False,
        ):
            add(f)
    for fn in dec.footnotes:
        if fn.inline:
            continue  # inline footnotes were scanned as part of their paragraph
        body = dec.text(fn.section)
        for f in _facts_in_block(dec, fn.section, 0, body, [(0, len(body))], (), footnote=True):
            add(f)
    facts.sort(key=lambda f: (f.section != "이유", f.section, f.char_start, f.kind))
    return facts


def _facts_in_block(
    dec: FtcDecision,
    section: str,
    offset: int,
    text: str,
    fn_spans: list[tuple[int, int]],
    table_refs: tuple[str, ...],
    *,
    footnote: bool,
) -> Iterator[CaseFact]:
    did = dec.decision_id

    def in_fn(abs_pos: int) -> bool:
        return footnote or any(a <= abs_pos < b for a, b in fn_spans)

    def mk(
        kind: str,
        m: re.Match[str],
        value: Any,
        conf: str,
        *,
        group: int | str = 0,
        masked: bool = False,
        mult: int = 1,
        note: str = "",
    ) -> CaseFact:
        s, e = m.span(group)
        ss, se = _sentence(text, s, e)
        return CaseFact(
            decision_id=did,
            kind=kind,
            value=value,
            raw=text[s:e],
            section=section,
            char_start=offset + s,
            char_end=offset + e,
            confidence=conf,
            masked=masked,
            in_footnote=in_fn(offset + s),
            context=re.sub(r"\s+", " ", text[ss:se]).strip()[:400],
            table_refs=_near_refs(text, s) or table_refs,
            unit_multiplier=mult,
            note=note,
        )

    # --- amounts: interest / unpaid interest / principal
    for m in _AMOUNT.finditer(text):
        raw_num = m.group("num")
        if not re.search(r"\d|\*", raw_num) or raw_num.strip(",") == "":
            continue
        mult = {"백만": 1_000_000, "천": 1000}.get(m.group("unit") or "", 1)
        n = parse_number(raw_num)
        value = n * mult if n is not None else None
        before = _before(text, m.start(), 45)
        after = _after(text, _skip_footnote(m.end(), fn_spans, offset), 25)
        sent_s, sent_e = _sentence(text, m.start(), m.end())
        sent = text[sent_s:sent_e]
        if re.search(r"과징금|어음할인료|수수료|인건비|판촉|광고비|장려금|매출액", before[-25:]):
            continue
        kind: str | None = None
        conf = "low"
        before_long = _before(text, m.start(), 70)
        if re.search(r"이자\s*(?:총|합계)?\s*[\d,]+\s*(?:천\s*)?원\s*중\s*$", before_long):
            # "지연이자 총 908,892,108원 중 853,285,820원을 지급하지 아니한" -> unpaid part
            kind = "total_unpaid_interest"
            conf = "high" if re.search(r"^\s*을?\s*(?:지급하지|미지급)", after) else "low"
        elif re.search(r"지연\s*이자[^,.]{0,12}$", before) or re.search(
            r"이자\s*(?:총|합계)?\s*$", before
        ):
            # "...원 중 ..." -> this is the total of which a part follows
            part_follows = bool(re.search(r"^\s*중\s", after))
            unpaid = not part_follows and bool(
                re.search(r"미지급", before[-20:])
                or re.search(r"^\s*을?\s*(?:지급하지|미지급|공탁하지)", after)
                or re.search(r"지급하지\s*아니", sent[m.end() - sent_s :][:60])
            )
            kind = "total_unpaid_interest" if unpaid else "total_interest"
            conf = "high" if re.search(r"지연\s*이자\s*(?:총|합계)?\s*$", before) else "low"
        elif re.search(
            r"(?:상품판매대금|상품대금|하도급대금|판매대금|미지급\s*대금|납품대금)[^,.]{0,15}$",
            before,
        ):
            kind = "total_delayed_principal"
            conf = "high" if re.search(r"(?:총|합계)\s*$", before) else "low"
        if kind is None:
            continue
        yield mk(
            kind,
            m,
            value,
            conf,
            masked=n is None,
            mult=mult,
            note=f"unit={m.group('unit') or ''}원",
        )

    # --- counts
    for m in _COUNT_SUPPLIER.finditer(text):
        n = parse_number(m.group("num"))
        yield mk(
            "supplier_count",
            m,
            n,
            "high" if "등" in _before(text, m.start(), 6) else "low",
            masked=n is None,
            note=m.group("who"),
        )
    for m in _COUNT_TXN.finditer(text):
        n = parse_number(m.group("num"))
        after = _after(text, m.end(), 20)
        before = _before(text, m.start(), 30)
        if re.search(r"계약|파견|서면|공문", before + after):
            continue
        yield mk("transaction_count", m, n, "low", masked=n is None)

    # --- delay day range
    for m in _DAYS_MINMAX.finditer(text):
        lo, hi = parse_number(m.group("lo")), parse_number(m.group("hi"))
        yield mk(
            "delay_days_range", m, {"min": lo, "max": hi}, "high", masked=lo is None or hi is None
        )
    for m in _DAYS_RANGE.finditer(text):
        if re.search(r"최소|최대", _before(text, m.start(), 8)):
            continue
        lo, hi = parse_number(m.group("lo")), parse_number(m.group("hi"))
        if re.search(
            r"지연|도과|초과|지나", _before(text, m.start(), 40) + _after(text, m.end(), 20)
        ):
            yield mk(
                "delay_days_range",
                m,
                {"min": lo, "max": hi},
                "low",
                masked=lo is None or hi is None,
            )
    for m in _DAYS_MAX.finditer(text):
        if _DAYS_MINMAX.search(text, max(0, m.start() - 30), m.end()):
            continue
        hi = parse_number(m.group("hi"))
        yield mk("delay_days_max", m, hi, "low", masked=hi is None)

    # --- interest rate
    for m in _RATE.finditer(text):
        sent_s, sent_e = _sentence(text, m.start(), m.end())
        sent = text[sent_s:sent_e]
        before = _before(text, m.start(), 30)
        if re.search(r"지연\s*이율|지연\s*이자|연리", sent):
            legal = bool(re.search(r"지연\s*이율|법정|고시", sent + before))
            # "법정 지연이자 연 15.5% 중 연 8%의 이자만", "나머지 연 7.5%" -> not the legal rate
            if re.search(r"(?:중|나머지|대신|아닌)\s*$", before.rstrip()[-8:] + "") or re.search(
                r"%\s*중\s*연?\s*$", before
            ):
                legal = False
            kind = "legal_interest_rate" if legal else "interest_rate_other"
            yield mk(
                kind,
                m,
                m.group("num"),
                "high" if legal else "low",
                note="value is percent per year",
            )

    # --- statutory term with base date ("상품수령일부터 60일")
    for m in _TERM.finditer(text):
        yield mk(
            "term_days",
            m,
            {"base": re.sub(r"\s+", "", m.group("base")), "days": int(m.group("days"))},
            "high",
        )

    # --- definition / rounding / holiday sentences
    for kind, rx in (
        ("base_date_definition", _BASE_DEF),
        ("rounding_note", _ROUNDING),
        ("holiday_note", _HOLIDAY),
    ):
        done: set[tuple[int, int]] = set()
        for m in rx.finditer(text):
            ss, se = _sentence(text, m.start(), m.end())
            if (ss, se) in done:
                continue
            sent = text[ss:se]
            if kind == "base_date_definition" and not re.search(
                r"기준|말한다|의미|이란|부터|으로부터|경과", sent
            ):
                continue
            if kind == "holiday_note" and not re.search(r"기한|기산|지연|지급", sent):
                continue
            done.add((ss, se))
            yield CaseFact(
                decision_id=did,
                kind=kind,
                value=re.sub(r"\s+", " ", sent).strip()[:400],
                raw=text[ss:se],
                section=section,
                char_start=offset + ss,
                char_end=offset + se,
                confidence="high" if in_fn(offset + m.start()) else "low",
                in_footnote=in_fn(offset + m.start()),
                context="",
                table_refs=table_refs,
                note=f"matched '{m.group(0)}'",
            )


def summarize(facts: list[CaseFact]) -> dict[str, Any]:
    """Counts per kind (+ masked) — used in manifests and the CLI."""
    out: dict[str, Any] = {}
    for f in facts:
        k = out.setdefault(f.kind, {"n": 0, "masked": 0, "low_confidence": 0})
        k["n"] += 1
        k["masked"] += int(f.masked)
        k["low_confidence"] += int(f.confidence == "low")
    return out
