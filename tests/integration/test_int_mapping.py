"""F2: the column-mapping contract (columns = indexes, options = values) through the API,
the worker and the real parser."""

from __future__ import annotations

import pytest

API = "/api/v1"
ROW = {"cp": "가나유통", "tt": "직매입", "ref": "PO-1", "amt": "1000", "rcv": "2025-08-07"}


def layout(order: list[str]) -> str:
    """A settlement sheet with unrecognisable headers in the given column order."""
    names = {
        "cp": "상대",
        "tt": "형태",
        "ref": "번호",
        "amt": "돈",
        "rcv": "받은날",
        "memo": "메모",
    }
    vals = {**ROW, "memo": "정산"}
    return ",".join(names[k] for k in order) + "\n" + ",".join(vals[k] for k in order) + "\n"


FIELD = {
    "cp": "counterparty",
    "tt": "trade_type",
    "ref": "reference",
    "amt": "amount",
    "rcv": "goods_received_date",
}


def columns(order: list[str]) -> dict[str, int]:
    return {FIELD[k]: i for i, k in enumerate(order) if k in FIELD}


def _apply(api, work, text: str, mapping: dict) -> dict:
    up = api.upload(text)
    work()
    assert api.version(up["document"]["id"])["status"] == "NEEDS_MAPPING"
    r = api.confirm_mapping(up["document"]["id"], mapping)
    assert r.status_code == 201, r.text
    work()
    res = api.job(r.json()["job_id"])
    assert res["status"] == "succeeded", res
    return res


@pytest.mark.parametrize(
    "order",
    [
        ["cp", "tt", "ref", "amt", "rcv"],  # counterparty column first
        ["memo", "cp", "tt", "ref", "amt", "rcv"],  # second (the reported UI case)
        ["tt", "ref", "amt", "rcv", "memo", "cp"],  # last
    ],
)
def test_counterparty_column_position(api, rt, tenant, work, order):
    mapping = {"format_id": "retail_settlement", "columns": columns(order)}
    res = _apply(api, work, layout(order), mapping)
    assert res["result"]["application"]["state"] == "applied"
    (line,) = rt.repos.ledger.list(tenant, "settlement_line")
    assert line.counterparty == "가나유통"
    assert line.amount.amount == 1000 and "counterparty" not in line.missing
    # the confirmed mapping is shown back in the same shape
    m = api.c.get(
        f"{API}/document-versions/{res['payload']['doc_version_id']}/mapping", headers=api.h
    ).json()
    assert m["confirmed"]["mapping"]["columns"] == columns(order)


def test_user_removes_the_counterparty_column(api, rt, tenant, work):
    order = ["memo", "cp", "tt", "ref", "amt", "rcv"]
    cols: dict = {**columns(order), "counterparty": None}
    _apply(api, work, layout(order), {"format_id": "retail_settlement", "columns": cols})
    (line,) = rt.repos.ledger.list(tenant, "settlement_line")
    assert line.counterparty == "" and "counterparty" in line.missing


def test_explicit_override_wins_over_the_column(api, rt, tenant, work):
    order = ["memo", "cp", "tt", "ref", "amt", "rcv"]
    mapping = {
        "format_id": "retail_settlement",
        "columns": columns(order),
        "options": {"counterparty_override": "  다라마트  "},
    }
    _apply(api, work, layout(order), mapping)
    (line,) = rt.repos.ledger.list(tenant, "settlement_line")
    assert line.counterparty == "다라마트"
    fact = rt.repos.facts.get(tenant, f"fact:{line.id}:counterparty_option")
    assert fact is not None and fact.value == "다라마트"


@pytest.mark.parametrize(
    "bad",
    [
        {"counterparty": 1, "amount": 4},  # retired flat shape
        {"columns": {"counterparty": "1"}},
        {"columns": {"amount": True}},
        {"columns": {"amount": 1, "counterparty": 1}},
        {"options": {"counterparty_override": 7}},
        {"options": {"account_override": ["x"]}},
        {"columns": {"Amount!": 1}},
    ],
)
def test_api_rejects_mappings_outside_the_contract(api, work, bad):
    up = api.upload(layout(["cp", "tt", "ref", "amt", "rcv"]))
    work()
    r = api.confirm_mapping(up["document"]["id"], bad)
    assert r.status_code == 422, r.text
    j = api.c.post(
        f"{API}/jobs",
        headers=api.h,
        json={
            "type": "ingest_document",
            "params": {"doc_version_id": up["document"]["id"], "mapping": bad},
        },
    )
    assert j.status_code == 422, j.text


def test_legacy_stored_flat_mapping_is_read_unambiguously(api, rt, tenant, work):
    """A mapping stored by the old UI ({counterparty: 1}) means column 1, not a name."""
    order = ["memo", "cp", "tt", "ref", "amt", "rcv"]
    up = api.upload(layout(order), auto_ingest=False)
    dvid = up["document"]["id"]
    legacy = {"format_id": "retail_settlement", **columns(order)}
    with rt.db.write(tenant):
        rt.mappings.confirm(tenant, dvid, legacy, "old-ui@x.example", "map_legacy")
    job = api.ingest(dvid)
    work()
    assert api.job(job)["status"] == "succeeded"
    (line,) = rt.repos.ledger.list(tenant, "settlement_line")
    assert line.counterparty == "가나유통"


def test_legacy_header_name_mapping_fails_the_job_with_a_clear_error(api, rt, tenant, work):
    up = api.upload(layout(["cp", "tt", "ref", "amt", "rcv"]), auto_ingest=False)
    dvid = up["document"]["id"]
    with rt.db.write(tenant):
        rt.mappings.confirm(tenant, dvid, {"amount": "돈"}, "old-ui@x.example", "map_hdr")
    job = api.ingest(dvid)
    work()
    res = api.job(job)
    assert res["status"] == "failed" and res["attempts"] == 1, res
    assert res["error"]["code"] == "bad_mapping"
    assert "confirm the mapping again" in res["error"]["message"]
    m = api.c.get(f"{API}/document-versions/{dvid}/mapping", headers=api.h).json()
    assert m["confirmed"]["mapping"] is None and m["confirmed"]["legacy_unreadable"] is True
    assert rt.repos.ledger.list(tenant, "settlement_line") == []
