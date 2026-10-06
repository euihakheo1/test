"""BPI 2019 pipeline on a tiny hand-written XES fixture (not real data)."""

from __future__ import annotations

import gzip
import hashlib
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from jettae.domain.money import Money
from jettae.evals import bpi_eval
from jettae.sources.bpi2019 import download
from jettae.sources.bpi2019.convert import (
    amount_minor,
    iter_items,
    read_items,
    write_items,
)
from jettae.sources.bpi2019.p2p import analyze_item
from jettae.sources.bpi2019.xes import iter_traces

FIX = Path(__file__).parent / "fixtures" / "bpi_tiny.xes"


@pytest.fixture()
def data_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JETTAE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JETTAE_REPO_ROOT", str(tmp_path))
    (tmp_path / "docs").mkdir()
    return tmp_path


def items_by_id() -> dict:
    return {it.id: it for it in iter_items(FIX)}


def test_streaming_parser_reads_traces_and_string_values() -> None:
    traces = list(iter_traces(FIX))
    assert [t.name for t in traces] == ["T1_00010", "T2_00010", "T3_00020", "T4_00010", "T5_00010"]
    t1 = traces[0]
    assert t1.attrs["Item Category"] == "3-way match, invoice after GR"
    assert [e.activity for e in t1.events] == [
        "Create Purchase Order Item",
        "Record Goods Receipt",
        "Record Invoice Receipt",
        "Clear Invoice",
    ]
    assert t1.events[1].attrs["Cumulative net worth (EUR)"] == "100.0"  # kept as string
    assert list(iter_traces(FIX, limit=2))[-1].name == "T2_00010"


def test_gz_and_zip_inputs(tmp_path: Path) -> None:
    gz = tmp_path / "log.xes.gz"
    gz.write_bytes(gzip.compress(FIX.read_bytes()))
    zp = tmp_path / "log.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.write(FIX, "BPI_Challenge_2019.xes")
    assert len(list(iter_traces(gz))) == 5
    assert len(list(iter_traces(zp))) == 5


def test_amount_parsing_uses_decimal_and_flags_rounding() -> None:
    assert amount_minor("250.5") == (Money(25050, "EUR"), False)
    assert amount_minor("0.125") == (Money(13, "EUR"), True)  # HALF_UP to cents
    assert amount_minor("1e3") == (Money(100000, "EUR"), False)
    assert amount_minor("abc") == (None, False)
    assert amount_minor(None) == (None, False)
    assert amount_minor("NaN") == (None, False)


def test_convert_roundtrip(tmp_path: Path) -> None:
    out = tmp_path / "items.jsonl.gz"
    assert write_items(iter_items(FIX), out) == 5
    back = list(read_items(out))
    assert [b.to_json() for b in back] == [a.to_json() for a in iter_items(FIX)]
    assert [b.id for b in iter_items(out, limit=2)] == ["T1_00010", "T2_00010"]


def test_item_linking_and_payment_days() -> None:
    it = items_by_id()
    a1 = analyze_item(it["T1_00010"])
    assert a1.gr_status == {"MATCHED": 1}
    assert a1.ir_status == {"MATCHED": 1}
    assert a1.pay_days_from_ir == [36]  # 2018-01-15 -> 2018-02-20 (local dates)
    assert a1.pay_days_from_gr == [41]  # 2018-01-10 -> 2018-02-20
    assert a1.invoices[0].goods_received_date is not None
    assert a1.invoices[0].amount == Money(10000, "EUR")

    a2 = analyze_item(it["T2_00010"])
    assert a2.gr_ir_diff == Money(5050, "EUR")  # IR 250.50 vs GR 200.00
    assert a2.has_ir and not a2.has_clear

    a4 = analyze_item(it["T4_00010"])  # two equal GRs and IRs: no FIFO guess
    assert a4.gr_status == {"AMBIGUOUS": 2}
    assert a4.gr_ir_conserved

    skipped = analyze_item(it["T4_00010"], max_events=1)
    assert not skipped.linked and skipped.n_gr == 2


def test_evaluate_descriptive_numbers() -> None:
    res = bpi_eval.evaluate(iter_items(FIX))
    o = res["overall"]
    assert res["log"]["items"] == 5 and res["log"]["events"] == 16
    assert o["items_invoiced_without_clearing"] == 2  # T2, T4
    assert o["gr_ir"]["items_with_both"] == 3
    assert o["gr_ir"]["items_amount_mismatch"] == 1
    assert o["gr_ir"]["abs_difference_eur_total"] == "50.50"
    assert o["payment_days_from_invoice_receipt"]["n"] == 2
    assert o["payment_days_from_invoice_receipt"]["median"] == 30
    assert o["conservation_violations"] == 0
    assert set(res["by_item_category"]) == {
        "2-way match",
        "3-way match, invoice after GR",
        "3-way match, invoice before GR",
        "Consignment",
    }
    json.dumps(res)  # serialisable, no Money / Decimal objects leak


def test_dist_nearest_rank() -> None:
    d = bpi_eval.dist(list(range(1, 11)))
    assert (d["min"], d["p10"], d["p25"], d["median"], d["p90"], d["max"]) == (1, 1, 3, 5, 9, 10)
    assert d["mean"] == "5.50"
    assert bpi_eval.dist([]) == {"n": 0}


def test_run_refuses_without_registered_real_log(data_tmp: Path) -> None:
    with pytest.raises(bpi_eval.NotRealData):
        bpi_eval.run(FIX)
    bpi_eval.record_pending("test reason", md=data_tmp / "docs" / "eval_results.md")
    text = (data_tmp / "docs" / "eval_results.md").read_text(encoding="utf-8")
    assert "**Not run**" in text and "test reason" in text


def test_run_on_registered_file_writes_results(data_tmp: Path) -> None:
    # registering a file only proves the pipeline wiring; the fixture is not real data
    local = data_tmp / "data" / "raw" / "bpi2019" / "x.xes"
    local.parent.mkdir(parents=True)
    local.write_bytes(FIX.read_bytes())
    tr, _ = _transport([], b"", maintenance=True)  # 4TU unreachable: md5 cannot be compared
    download.register_local_file(local, client=httpx.Client(transport=tr))
    assert download.load_manifest()["status"] == "unverified_local"
    res = bpi_eval.run(None, out=data_tmp / "r.json", md=data_tmp / "docs" / "e.md")
    assert res["run"]["input_sha256"] == hashlib.sha256(FIX.read_bytes()).hexdigest()
    text = (data_tmp / "docs" / "e.md").read_text(encoding="utf-8")
    assert "E4" in text and "unverified local file" in text


def test_register_local_file_checks_published_md5(data_tmp: Path) -> None:
    local = data_tmp / "x.xes"
    local.write_bytes(FIX.read_bytes())
    good = [
        {
            "name": "BPI_Challenge_2019.xes",
            "download_url": "https://data.4tu.nl/file/x/BPI_Challenge_2019.xes",
            "supplied_md5": hashlib.md5(FIX.read_bytes()).hexdigest(),
        }
    ]
    tr, _ = _transport(good, b"")
    download.register_local_file(local, client=httpx.Client(transport=tr))
    m = download.load_manifest()
    assert m["status"] == "local_file" and download.provenance_warning(m) is None
    bad = [dict(good[0], supplied_md5="0" * 32)]
    tr, _ = _transport(bad, b"")
    with pytest.raises(download.ProvenanceMismatch):
        download.register_local_file(local, client=httpx.Client(transport=tr))
    with pytest.raises(download.ProvenanceMismatch):
        download.register_local_file(local, expected_md5="1" * 32, check_remote=False)


# ------------------------------------------------------------------ downloader
def _transport(files_json: object, payload: bytes, maintenance: bool = False):
    calls = {"files": 0, "download": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.startswith("https://doi.org/"):
            return httpx.Response(302, headers={"location": "https://data.4tu.nl/articles/_/999/1"})
        if url.endswith("/v2/articles/999/files"):
            calls["files"] += 1
            if maintenance:
                return httpx.Response(200, json={"status": "maintenance"})
            return httpx.Response(200, json=files_json)
        if url == "https://data.4tu.nl/file/x/BPI_Challenge_2019.xes":
            calls["download"] += 1
            return httpx.Response(
                200, content=payload, headers={"content-type": "application/octet-stream"}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def test_fetch_detects_maintenance_and_backs_off(data_tmp: Path) -> None:
    tr, calls = _transport([], b"", maintenance=True)
    waits: list[float] = []
    with pytest.raises(download.SourceUnavailable) as ei:
        download.fetch(
            retries=3,
            backoff_s=2.0,
            client=httpx.Client(transport=tr),
            sleep=waits.append,
            log=lambda s: None,
        )
    assert waits == [2.0, 4.0]
    assert calls["files"] == 3
    assert "maintenance" in str(ei.value) and "--file" in str(ei.value)
    m = download.load_manifest()
    assert m["status"] == "unavailable"
    assert [a["status"] for a in m["attempts"]] == ["maintenance"] * 3


def test_fetch_detects_html_maintenance_page(data_tmp: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "doi.org" in str(req.url):
            return httpx.Response(302, headers={"location": "https://data.4tu.nl/articles/_/999/1"})
        return httpx.Response(
            200,
            text="<html><body>The repository is offline for maintenance.</body></html>",
            headers={"content-type": "text/html"},
        )

    with pytest.raises(download.SourceUnavailable):
        download.fetch(
            retries=1,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda s: None,
            log=lambda s: None,
        )


def test_fetch_success_verifies_md5_and_records_manifest(data_tmp: Path) -> None:
    payload = FIX.read_bytes()
    files = [
        {"name": "readme.txt", "download_url": "https://data.4tu.nl/file/x/readme.txt", "size": 3},
        {
            "name": "BPI_Challenge_2019.xes",
            "download_url": "https://data.4tu.nl/file/x/BPI_Challenge_2019.xes",
            "size": len(payload),
            "supplied_md5": hashlib.md5(payload).hexdigest(),
        },
    ]
    tr, calls = _transport(files, payload)
    p = download.fetch(retries=1, client=httpx.Client(transport=tr), log=lambda s: None)
    assert p.read_bytes() == payload
    m = download.load_manifest()
    assert m["status"] == "ok"
    assert m["file"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert m["license"] == "CC BY 4.0"
    assert download.local_log_path() == p
    # second fetch: md5 of the present file verified, no new download
    download.fetch(retries=1, client=httpx.Client(transport=tr), log=lambda s: None)
    assert calls["download"] == 1


def test_fetch_md5_mismatch_is_an_error(data_tmp: Path) -> None:
    files = [
        {
            "name": "BPI_Challenge_2019.xes",
            "download_url": "https://data.4tu.nl/file/x/BPI_Challenge_2019.xes",
            "supplied_md5": "0" * 32,
        }
    ]
    tr, _ = _transport(files, b"<log/>")
    with pytest.raises(download.SourceUnavailable) as ei:
        download.fetch(retries=1, client=httpx.Client(transport=tr), log=lambda s: None)
    assert "md5 mismatch" in str(ei.value)
    assert not list((data_tmp / "data" / "raw" / "bpi2019").glob("*.xes"))
