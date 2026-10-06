"""API → worker → private-model protocol → tools; mock HTTP is not model-quality evidence."""

from __future__ import annotations

import json

import httpx
import openai
from aw_helpers import FILES, Web
from jt_api_helpers import signup

from jettae.llm.vllm import VLLMProvider


def test_local_model_web_investigation_uses_tools_at_zero_paid_budget(
    client, rt, work, monkeypatch
):
    web = Web(client, signup(client, "local@x.example", "local"))
    for text, kind, name in FILES:
        web.upload(text, kind, name)
    work()
    web.analyze()
    work()
    did = next(iter(web.decisions()))
    before = web.decisions()[did]["result_hash"]
    monkeypatch.setenv("JETTAE_LLM_MODE", "local")
    monkeypatch.setenv("JETTAE_LLM_PROVIDER", "vllm")
    monkeypatch.setenv("JETTAE_LLM_BUDGET_KRW", "0")
    monkeypatch.setenv("JETTAE_LLM_CACHE_DIR", str(rt.settings.blob_dir.parent / "llm"))
    calls = []

    def reply(req):
        body = json.loads(req.content)
        state = json.loads(body["messages"][1]["content"][0]["text"])
        tool = "calculate_due" if not state["OBSERVATIONS"] else None
        args = {
            k: None
            for k in body["response_format"]["json_schema"]["schema"]["properties"]["args"][
                "properties"
            ]
        }
        args["decision_id"] = state["DECISION_ID"]
        action = {
            "action": "call_tool" if tool else "finish",
            "tool": tool,
            "args": args,
            "summary": "test",
        }
        calls.append(body)
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "jettae-local",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(action)},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    original = VLLMProvider.__init__

    def init(self, model=None, **kw):
        kw["client"] = openai.OpenAI(
            base_url="http://127.0.0.1:8001/v1",
            api_key="test-key",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(reply)),
        )
        original(self, model, **kw)

    monkeypatch.setattr(VLLMProvider, "__init__", init)
    caps = client.get("/api/v1/investigations/capabilities", headers=web.h).json()
    assert caps["local_enabled"] and not caps["live_enabled"]
    started = web.start(did, mode="local", strategy="single")
    assert started.status_code == 202
    work()
    inv = web.investigation(did, started.json()["investigation_id"])
    assert inv["status"] == "succeeded", inv
    assert len(calls) == inv["usage"]["llm_calls"] == 2
    assert inv["usage"]["cost_krw"] == 0 and inv["usage"]["tool_calls"] == 1
    assert [a["tool"] for a in inv["report"]["actions"] if a["kind"] == "tool_call"] == [
        "calculate_due"
    ]
    assert web.decisions()[did]["result_hash"] == before


def test_browser_cannot_enable_local_model_without_server_configuration(client, rt, work):
    web = Web(client, signup(client, "disabled@x.example", "disabled"))
    for text, kind, name in FILES:
        web.upload(text, kind, name)
    work()
    web.analyze()
    work()
    did = next(iter(web.decisions()))
    started = web.start(did, mode="local")
    work()
    inv = web.investigation(did, started.json()["investigation_id"])
    assert inv["status"] == "refused" and inv["usage"]["llm_calls"] == 0
