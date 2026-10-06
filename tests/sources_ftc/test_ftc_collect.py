"""Collection pipeline (search -> fetch -> parse -> manifest/facts/images) with a mock DRF."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from ftc_test_helpers import fixture_bytes

from jettae.sources.ftc.client import DrfClient
from jettae.sources.ftc.collect import QuerySpec, download_images, fetch

FIXTURE_BY_ID = {"16947": "f16947.xml", "19065": "x19065.xml"}


def _handler(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path.endswith("lawSearch.do"):
        items = [
            {
                "결정문일련번호": i,
                "사건번호": f"C{i}",
                "사건명": f"사건 {i}",
                "결정번호": "x",
                "결정일자": "2026.1.1.",
                "문서유형": "의결서",
                "회의종류": "x",
            }
            for i in FIXTURE_BY_ID
        ]
        body = {"Ftc": {"totalCnt": str(len(items)), "ftc": items}}
        return httpx.Response(200, content=json.dumps(body, ensure_ascii=False).encode())
    if path.endswith("lawService.do"):
        return httpx.Response(200, content=fixture_bytes(FIXTURE_BY_ID[req.url.params["ID"]]))
    if path.endswith("flDownload.do"):
        return httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\n" + req.url.params["flSeq"].encode(),
            headers={"content-type": "image/png"},
        )
    return httpx.Response(404)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JETTAE_DATA_DIR", str(tmp_path))
    return tmp_path


def _client(data_dir: Path) -> DrfClient:
    return DrfClient(
        oc="test",
        cache_dir=data_dir / "raw" / "ftc",
        transport=httpx.MockTransport(_handler),
        min_interval=0,
        sleep=lambda s: None,
    )


def test_fetch_writes_manifest_and_facts_and_limit_run_merges(data_dir: Path) -> None:
    q = (QuerySpec("t", "대규모유통", body=False),)
    with _client(data_dir) as c:
        rep = fetch(c, queries=q)
    assert [d["status"] for d in rep.decisions] == ["ok", "ok"]
    man = json.loads((data_dir / "manifests" / "ftc.json").read_text(encoding="utf-8"))
    assert man["counts"]["ok_total"] == 2
    d0 = man["decisions"][0]
    assert len(d0["sha256"]) == 64 and "OC={OC}" in d0["url"] and d0["fetched_at"]
    facts_path = data_dir / "seeds" / "ftc_case_facts.jsonl"
    ids = {json.loads(line)["decision_id"] for line in facts_path.read_text("utf-8").splitlines()}
    assert ids == {"16947", "19065"}

    # a later --limit 1 run keeps the other decision in manifest and facts
    with _client(data_dir) as c:
        fetch(c, queries=q, limit=1)
    man = json.loads((data_dir / "manifests" / "ftc.json").read_text(encoding="utf-8"))
    assert man["counts"]["decisions_total"] == 2 and man["counts"]["selected_this_run"] == 1
    assert sorted(d["in_last_run"] for d in man["decisions"]) == [False, True]
    ids = {json.loads(line)["decision_id"] for line in facts_path.read_text("utf-8").splitlines()}
    assert ids == {"16947", "19065"}


def test_download_images_selects_delay_tables(data_dir: Path) -> None:
    with _client(data_dir) as c:
        fetch(c, queries=(QuerySpec("t", "q", body=False),))
        out = download_images(c)
    seqs = {i["flseq"] for i in out["images"] if i["status"] == "ok"}
    # 이마트 표 5/6 and 홈플러스 표 11-15 are delay/interest tables; market tables are not
    assert {"133422117", "133422119", "163492277", "163492279", "163492283"} <= seqs
    assert "133422115" not in seqs and "163492275" not in seqs
    man = json.loads((data_dir / "manifests" / "ftc_images.json").read_text(encoding="utf-8"))
    assert man["counts"]["ok"] == len(seqs)
    assert all((data_dir / i["path"]).exists() for i in man["images"] if i["status"] == "ok")
