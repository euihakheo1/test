"""HWP5/HWPX readers, clause extraction and FTC board parsing on hand-written fixtures."""

# ruff: noqa: E501  (fixtures quote statute / board HTML lines verbatim)

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sm_builders import build_cfb, build_hwp, build_hwpx

from jettae.evals import contract_eval
from jettae.sources.contract import ftc_board
from jettae.sources.contract.cfb import CfbError, CompoundFile
from jettae.sources.contract.clauses import contract_type, extract
from jettae.sources.contract.hwp5 import HwpError, Paragraph, read_hwp
from jettae.sources.contract.textx import UnsupportedDocument, detect, read_document

ART = "제6조 [납품대금 지급 및 감액금지]"
CLAUSE = "① “갑”은 납품대금을 상품수령일로부터 (  )일 이내에 현금으로 지급한다."
NOTE = "※ 납품대금은 상품수령일로부터 60일 이내 지급하여야 함"


def test_cfb_reads_mini_and_regular_streams() -> None:
    big = bytes(range(256)) * 20  # 5120 bytes -> regular sectors
    data = build_cfb({"A": b"hello", "S/B": big, "S/C": b"x" * 70})
    cf = CompoundFile(data)
    assert cf.list_streams() == ["A", "S/B", "S/C"]
    assert cf.read("A") == b"hello"
    assert cf.read("S/B") == big
    assert cf.read("S/C") == b"x" * 70
    with pytest.raises(CfbError):
        CompoundFile(b"not an ole file" * 100)


def test_hwp5_text_with_controls() -> None:
    data = build_hwp(
        [
            (0, [ART]),
            (0, [11, CLAUSE]),  # extended control (table/shape) occupies 8 code units
            (1, ["셀 텍스트", 9, "탭 뒤"]),  # tab = inline control
            (0, [NOTE, 10, "다음 줄"]),
        ]
    )
    paras = read_hwp(data)
    assert [p.text for p in paras] == [ART, CLAUSE, "셀 텍스트\t탭 뒤", NOTE + "\n다음 줄"]
    assert [p.level for p in paras] == [0, 0, 1, 0]
    big_doc = build_hwp([(0, [ART])], big=True)
    cf = CompoundFile(big_doc)
    assert cf.paths["BodyText/Section0"].size >= cf.cutoff  # regular-sector stream path
    big = read_hwp(big_doc)
    assert big[0].text == ART and len(big) == 2


def test_hwp5_uncompressed_and_refusals() -> None:
    assert read_hwp(build_hwp([(0, ["평문"])], props=0))[0].text == "평문"
    with pytest.raises(HwpError, match="distribution"):
        read_hwp(build_hwp([(0, ["x"])], props=1 | 4))
    with pytest.raises(HwpError, match="password"):
        read_hwp(build_hwp([(0, ["x"])], props=1 | 2))
    with pytest.raises(HwpError):
        read_hwp(build_cfb({"Other": b"x"}))


def test_hwpx_paragraphs_including_table_cells() -> None:
    xml = (
        f"<hp:p><hp:run><hp:t>{ART}</hp:t></hp:run></hp:p>"
        "<hp:p><hp:run><hp:t>① “갑”은 상품판매대금을 </hp:t><hp:t>판매마감일부터 40일 이내에 지급한다.</hp:t>"
        "<hp:tbl><hp:tc><hp:p><hp:run><hp:t>셀</hp:t></hp:run></hp:p></hp:tc></hp:tbl>"
        "</hp:run></hp:p>"
    )
    kind, paras = read_document(build_hwpx(xml), "x.hwpx")
    assert kind == "hwpx"
    assert [p.text for p in paras] == [
        ART,
        "① “갑”은 상품판매대금을 판매마감일부터 40일 이내에 지급한다.",
        "셀",
    ]
    assert paras[2].level == 1


def test_detect_and_unsupported() -> None:
    assert detect(b"%PDF-1.7 ...") == "pdf"
    assert detect(b"PK\x03\x04...") == "hwpx"
    with pytest.raises(UnsupportedDocument):
        read_document(b"plain text", "a.txt")


def P(i: int, text: str, level: int = 0) -> Paragraph:
    return Paragraph(0, i, level, text)


def test_extract_clauses_with_spans() -> None:
    paras = [
        P(0, ART),
        P(1, CLAUSE),
        P(2, NOTE, 2),
        P(3, " ② “갑”은 대금을 감액하지 아니한다."),
        P(4, "제7조 [상품의 반품]"),
        P(
            5, " ② 반품 기간은 납품일로부터 (  )일 이내로 한다."
        ),  # no 지급/대금 -> not a payment clause
        P(6, "① “갑”은 납품대금을 상품 입고일부터 60일 이내에 지급한다."),
    ]
    ex = extract(paras)
    assert ex.articles == [("6", "납품대금 지급 및 감액금지"), ("7", "상품의 반품")]
    c, note, other = ex.clauses
    assert c.kind == "clause" and c.article_no == "6"
    assert c.base_kind == "goods_received_date" and c.term_blank and c.term_days is None
    assert c.text[c.base_span[0] : c.base_span[1]] == "상품수령일로부터"  # type: ignore[index]
    assert c.text[c.term_span[0] : c.term_span[1]] == "(  )일 이내"  # type: ignore[index]
    assert ex.text[c.locator["char_start"] : c.locator["char_end"]] == CLAUSE
    assert note.kind == "drafting_note" and note.term_days == 60
    assert other.article_no == "7" and other.base_kind == "goods_in_date"
    assert other.term_days == 60


def test_contract_type_and_assessment() -> None:
    assert contract_type("(개정) 직매입 표준거래계약서 (편의점)") == "direct"
    assert contract_type("온라인쇼핑몰 표준거래계약서 (위수탁거래)") == "consignment"
    assert contract_type("TV홈쇼핑 표준거래계약서") is None
    exp = contract_eval.expectations()
    assert exp["direct"]["term_days"] == 60 and exp["consignment"]["term_days"] == 40
    ex = extract([P(0, ART), P(1, CLAUSE), P(2, NOTE, 2)])
    a = contract_eval.assess("직매입 표준거래계약서", ex, exp)
    assert a["base_consistent"] and a["term_blank"] and a["note_matches_statute"]
    a2 = contract_eval.assess("특약매입 표준거래계약서", ex, exp)
    assert a2["base_consistent"] is False and "base_note" in a2


def test_evaluate_documents_from_files(tmp_path: Path) -> None:
    data = build_hwp([(0, [ART]), (0, [CLAUSE]), (2, [NOTE])])
    (tmp_path / "f.hwp").write_bytes(data)
    (tmp_path / "bad.hwp").write_bytes(b"garbage" * 100)
    docs = [
        {
            "ntt": "1",
            "title": "직매입 표준거래계약서",
            "ok": True,
            "path": "f.hwp",
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        {"ntt": "2", "title": "특약매입 표준거래계약서", "ok": True, "path": "bad.hwp"},
        {"ntt": "3", "title": "직매입", "ok": False, "error": "HTTP 500"},
        {"ntt": "4", "title": "직매입", "ok": True, "path": "f.hwp", "sha256": "0" * 64},
    ]
    res = contract_eval.evaluate_documents(docs, root=tmp_path)
    st = [r["status"] for r in res["documents"]]
    assert st == ["extracted", "unsupported", "not_downloaded", "hash_mismatch"]
    assert res["summary"]["extracted"] == 1 and res["summary"]["base_consistent"] == 1
    assert res["summary"]["broad_filter_paragraphs"] == 2
    assert res["summary"]["broad_filter_paragraphs_extracted"] == 2


LIST_HTML = """<table><tbody>
<tr><td>13</td><td class="p-subject"><a href="./selectBbsNttView.do?pageUnit=10&amp;key=205&amp;bordCd=204&amp;nttSn=46669">
<span class="p-table__text">온라인쇼핑몰 표준거래계약서(직매입거래)(25.11월)</span></a></td>
<td>유통대리점정책과</td><td>2025-11-25</td><td class="p-file"><a href="./downloadBbsFile.do?atchmnflNo=52131">파일다운로드</a></td></tr>
<tr><td>3</td><td class="p-subject"><a href="./selectBbsNttView.do?key=205&amp;nttSn=11352">
<span>(개정) TV홈쇼핑 표준거래계약서</span></a></td><td>과</td><td>2024-11-28</td></tr>
</tbody></table>"""

VIEW_HTML = """<div><a href="./downloadBbsFile.do?atchmnflNo=52129" class="p-attach__link">25년 개정 직매입 표준거래계약서(백화점, 대형마트).hwp &nbsp; <span class="p-attach__size">(hwp, 106.5KB)</span></a>
<a href="./previewBbsAtchmnfl.do?key=205&fileNo=52129" class="btn">문서뷰어</a>
<a href="./downloadBbsFile.do?atchmnflNo=52130" class="p-attach__link">같은 서식.pdf <span class="p-attach__size">(pdf, 300KB)</span></a></div>"""


def test_board_parsing() -> None:
    posts = ftc_board.parse_list(LIST_HTML)
    assert [(p.ntt, p.registered) for p in posts] == [
        ("46669", "2025-11-25"),
        ("11352", "2024-11-28"),
    ]
    assert posts[0].title == "온라인쇼핑몰 표준거래계약서(직매입거래)(25.11월)"
    assert [p.title for p in posts if ftc_board.TITLE_RE.search(p.title)] == [posts[0].title]
    atts = ftc_board.parse_attachments(VIEW_HTML)
    assert [(a.file_no, a.ext) for a in atts] == [("52129", "hwp"), ("52130", "pdf")]
    assert atts[0].name == "25년 개정 직매입 표준거래계약서(백화점, 대형마트).hwp"
    best = sorted(atts, key=lambda a: (ftc_board.FORMAT_RANK.get(a.ext, 9), a.file_no))[0]
    assert best.ext == "pdf"  # PDF preferred over HWP when both exist


def test_run_without_downloaded_forms_refuses_and_keeps_the_summary(tmp_path: Path, monkeypatch):
    """A fresh clone has the manifest but not data/raw/: the reviewed E5 block must not be
    replaced by 'file_missing' rows, and the command must exit non-zero (like eval bpi)."""
    import pytest

    docs = [{"ntt": "1", "title": "직매입", "ok": True, "path": "data/raw/contract/1.hwp"}]
    monkeypatch.setattr(contract_eval.ftc_board, "load_manifest", lambda: {"documents": docs})
    monkeypatch.setattr(contract_eval, "repo_root", lambda: tmp_path)
    md = tmp_path / "eval_results.md"
    md.write_text("## E5 — reviewed\n\nkeep me\n", encoding="utf-8")
    out = tmp_path / "contract_eval.json"
    with pytest.raises(contract_eval.ContractInputsMissing):
        contract_eval.run(out=out, md=md)
    assert md.read_text(encoding="utf-8") == "## E5 — reviewed\n\nkeep me\n"
    assert not out.exists()

    def missing(**kw):
        raise contract_eval.ContractInputsMissing("x")

    monkeypatch.setattr(contract_eval, "run", missing)
    with pytest.raises(SystemExit) as e:
        contract_eval.run_cli()
    assert e.value.code == 3
