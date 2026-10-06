import json

from ing_helpers import scanned_pdf
from typer.testing import CliRunner

from jettae.cli import MODULE_STATUS
from jettae.cli import app as root

runner = CliRunner()
BANK = (
    "거래일시,적요,기재내용,찾으신금액,맡기신금액,잔액,거래점\n"
    '2025.10.20 10:15:00,타행이체,가나유통,0,"9,900,000","12,000,000",본점\n'
).encode("cp949")


def test_ingest_subapp_is_registered():
    assert MODULE_STATUS.get("jettae.ingest.cli") == "loaded"
    r = runner.invoke(root, ["ingest", "--help"])
    assert r.exit_code == 0 and "inspect" in r.output and "parse" in r.output


def test_inspect_json(tmp_path):
    f = tmp_path / "kb.csv"
    f.write_bytes(BANK)
    r = runner.invoke(root, ["ingest", "inspect", str(f), "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["status"] == "PARSED" and data["encoding"] == "cp949"
    (t,) = data["tables"]
    assert t["format"] == "kr_bank_txn"
    fields = {m["field"]: m["confidence"] for m in t["mapping"]["matches"]}
    assert fields["deposit"] == "high" and fields["withdrawal"] == "high"
    r2 = runner.invoke(root, ["ingest", "inspect", str(f)])
    assert r2.exit_code == 0 and "kr_bank_txn" in r2.output


def test_parse_writes_rows_records_and_facts(tmp_path):
    f = tmp_path / "kb.csv"
    f.write_bytes(BANK)
    out = tmp_path / "rows.json"
    r = runner.invoke(root, ["ingest", "parse", str(f), "--out", str(out)])
    assert r.exit_code == 0, r.output
    data = json.loads(out.read_text(encoding="utf-8"))
    rows = data["tables"][0]["rows"]
    assert rows[1]["cells"][3]["locator"]["char_start"] > 0
    assert len(data["records"]) == 1
    rec = data["records"][0]
    assert rec["$type"] == "BankTxn" and rec["amount"] == {"$money": [9900000, "KRW"]}
    assert any(fct["kind"] == "bank_txn.deposit" for fct in data["facts"])


def test_parse_exit_codes_for_mapping_and_scan(tmp_path):
    f = tmp_path / "x.csv"
    f.write_bytes("날,돈\n2025-01-02,100\n".encode())
    out = tmp_path / "o.json"
    r = runner.invoke(root, ["ingest", "parse", str(f), "--out", str(out)])
    assert r.exit_code == 2
    r2 = runner.invoke(
        root,
        [
            "ingest",
            "parse",
            str(f),
            "--out",
            str(out),
            "--format",
            "kr_bank_txn",
            "--map",
            "txn_date=날",
            "--map",
            "deposit=1",
        ],
    )
    assert r2.exit_code == 0, r2.output
    assert len(json.loads(out.read_text(encoding="utf-8"))["records"]) == 1
    s = tmp_path / "scan.pdf"
    s.write_bytes(scanned_pdf())
    r3 = runner.invoke(root, ["ingest", "inspect", str(s), "--json"])
    assert r3.exit_code == 3 and json.loads(r3.output)["status"] == "UNSUPPORTED_SCAN"
