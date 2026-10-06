"""Agent investigation through API + worker with the real parser: offline heuristic planner,
single vs roles, findings per decision status, engine numbers unchanged."""

from __future__ import annotations

from typing import Any

from aw_helpers import FILES, Web, seed_conflict, tenant_of
from jt_api_helpers import signup

from jettae.verify.checks import check_wording


def _setup(client: Any, work: Any) -> tuple[Web, dict[str, dict[str, Any]]]:
    web = Web(client, signup(client, "flow@x.example", "가 회사"))
    for text, kind, name in FILES:
        web.upload(text, kind, name)
    work()
    job = web.analyze()
    work()
    assert web.job(job)["status"] == "succeeded"
    return web, web.decisions()


def _by_status(decs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in decs.values():
        out.setdefault(d["status"], d)
    return out


def _run(web: Web, work: Any, did: str, strategy: str) -> dict[str, Any]:
    r = web.start(did, strategy)
    assert r.status_code == 202, r.text
    body = r.json()
    assert set(body) >= {"investigation_id", "job_id"}
    assert web.investigation(did, body["investigation_id"])["status"] == "queued"
    work()
    assert web.job(body["job_id"])["status"] == "succeeded"
    return web.investigation(did, body["investigation_id"])


def _assert_citations(findings: list[dict[str, Any]]) -> None:
    for f in findings:
        assert f["citations"], f
        for c in f["citations"]:
            assert c["doc_version_id"] and c["excerpt"] and isinstance(c["locator"], dict)


def test_offline_investigations_follow_decision_status(client, rt, work):
    web, decs = _setup(client, work)
    st = _by_status(decs)
    assert {"MATCHED", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE"} <= set(st), sorted(st)
    before = {d: (x["result_hash"], x["status"]) for d, x in decs.items()}

    results: dict[str, dict[str, dict[str, Any]]] = {}
    for status in ("MATCHED", "AMBIGUOUS", "INSUFFICIENT_EVIDENCE"):
        did = st[status]["id"]
        results[status] = {s: _run(web, work, did, s) for s in ("single", "roles")}

    for status, by_strategy in results.items():
        single, roles = by_strategy["single"], by_strategy["roles"]
        assert single["status"] == roles["status"] == "succeeded", (status, single, roles)
        # same tools, same engine: same findings whatever the strategy
        assert single["findings"] == roles["findings"]
        assert single["strategy"] == "single" and roles["strategy"] == "roles"
        for inv in (single, roles):
            u = inv["usage"]
            assert u["steps"] > 0 and u["tool_calls"] > 0
            assert u["input_tokens"] == u["output_tokens"] == 0 and u["cost_krw"] == 0
            assert u["llm_calls"] == 0
            assert inv["report"]["planner"] == "heuristic"
            for f in inv["findings"]:
                assert check_wording(f["message"]).passed, f["message"]
        _assert_citations(single["findings"])

    # MATCHED: nothing is missing
    # MATCHED: nothing is missing (a holiday-rollover condition may still be open: that is
    # an unconfirmed calculation condition, not a missing document)
    f, *rest = results["MATCHED"]["single"]["findings"]
    assert f["kind"] == "nothing_missing" and f["required_documents"] == []
    assert all(x["kind"] == "condition_unconfirmed" and not x["required_documents"] for x in rest)

    # AMBIGUOUS document link: candidate lines + same_sale / separate_sale confirmation
    amb = st["AMBIGUOUS"]
    detail = web.detail(amb["id"])
    ev = next(c for c in detail["computations"] if c["name"] == "evidence")["outputs"]
    kinds = [f["kind"] for f in results["AMBIGUOUS"]["single"]["findings"]]
    assert kinds[0] == "confirmation_required", kinds
    conf = results["AMBIGUOUS"]["single"]["findings"][0]
    cands = ev["confirmation_required"]["candidate_settlement_lines"]
    assert [c["id"] for c in conf["details"]["candidates"]] == cands
    assert conf["details"]["choices"] == ["same_sale", "separate_sale"]
    assert "same_sale" in conf["message"] and "separate_sale" in conf["message"]
    assert all(f"[{c}]" in conf["message"] for c in cands)
    assert "11,000,000원" in conf["message"]
    assert set(conf["required_documents"]) <= set(detail["required_documents"])
    cited_docs = {c["doc_version_id"] for c in conf["citations"]}
    assert len(cited_docs) == 2  # the invoice and the candidate statement line

    # INSUFFICIENT_EVIDENCE: required documents (verbatim) with the facts used
    ins = st["INSUFFICIENT_EVIDENCE"]
    detail = web.detail(ins["id"])
    findings = results["INSUFFICIENT_EVIDENCE"]["single"]["findings"]
    req = [f for f in findings if f["kind"] == "required_documents"]
    assert req and "상품수령일" in req[0]["message"]
    assert "세금계산서 작성일로 대신 계산하지 않습니다" in req[0]["message"]
    shown = [d for f in findings for d in f["required_documents"]]
    assert sorted(shown) == sorted(detail["required_documents"]) and shown

    # engine results, approvals and the ledger are untouched by the investigations
    after = {d: (x["result_hash"], x["status"]) for d, x in web.decisions().items()}
    assert after == before
    tenant = tenant_of(client, web.h)
    assert all(rt.repos.approvals.list_for(tenant, d) == [] for d in decs)


def test_conflict_shows_both_amounts_with_spans(client, rt, work):
    web = Web(client, signup(client, "conflict@x.example", "다 회사"))
    tenant = tenant_of(client, web.h)
    docs = seed_conflict(rt, tenant)
    web.analyze()
    work()
    (dec,) = web.decisions().values()
    assert dec["status"] == "CONFLICT"
    inv = _run(web, work, dec["id"], "roles")
    assert inv["status"] == "succeeded"
    f = inv["findings"][0]
    assert f["kind"] == "amount_conflict"
    assert "[S1] 10,000원" in f["message"] and "[I1] 9,000원" in f["message"]
    assert [a["id"] for a in f["details"]["amounts"]] == ["S1", "I1"]
    by_doc = {c["doc_version_id"]: c for c in f["citations"]}
    assert set(by_doc) == {docs["settle_doc"], docs["invoice_doc"]}
    assert all(c["found_in_source"] for c in f["citations"])
    assert f["required_documents"] and all("금액" in d for d in f["required_documents"])
    assert check_wording(f["message"]).passed
    assert web.decisions()[dec["id"]]["result_hash"] == dec["result_hash"]
