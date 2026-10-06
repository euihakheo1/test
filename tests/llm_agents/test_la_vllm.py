"""Private inference, zero paid budget, schema validation and replay; no GPU or network.

The HTTP responses here are handwritten protocol fixtures, not model-quality evidence.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import openai
import pytest

from jettae.llm import Budget, GatewayConfig, LLMGateway, LLMMode, MemoryReplayStore
from jettae.llm.base import ImagePart, LiveCallRefused, LLMError, LLMMessage, LLMRequest
from jettae.llm.gateway import gateway_from_env
from jettae.llm.vllm import VLLMProvider, validate_base_url


def request():
    return LLMRequest(
        purpose="test",
        system="Return an answer",
        messages=(LLMMessage.user("test"),),
        schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        tenant_id="test",
        contains_tenant_data=True,
        prompt_id="test",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "http://8.8.8.8/v1",
        "http://0.0.0.0/v1",
        "http://169.254.169.254/v1",
        "http://127.0.0.1:8001/v1?key=private",
        "http://user:test-password@localhost/v1",
        "http://localhost:broken/v1",
        "http://localhost/not-v1",
    ],
)
def test_public_or_credential_bearing_endpoint_is_refused(url):
    with pytest.raises(LLMError, match="JETTAE_VLLM_BASE_URL") as exc:
        validate_base_url(url)
    assert "private" not in str(exc.value).replace("private/loopback", "")


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8001/v1", "http://vllm:8001/v1", "http://10.0.0.2/v1"]
)
def test_operator_controlled_endpoint(url):
    assert validate_base_url(url) == url


def test_local_mode_cannot_select_paid_provider(tmp_path):
    with pytest.raises(LiveCallRefused):
        gateway_from_env(
            env={
                "JETTAE_LLM_MODE": "local",
                "JETTAE_LLM_PROVIDER": "openai",
                "JETTAE_OPENAI_MODEL": "test-model",
            }
        )


def test_local_inference_and_replay_at_zero_paid_budget():
    calls = []

    def reply(req):
        body = json.loads(req.content)
        calls.append(body)
        assert body["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "jettae-local",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"answer":"ok"}'},
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    client = openai.OpenAI(
        base_url="http://127.0.0.1:8001/v1",
        api_key="test-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(reply), trust_env=False),
    )
    p = VLLMProvider(client=client)
    gw = LLMGateway.for_provider(
        p,
        MemoryReplayStore(),
        budget=Budget(Decimal(0)),
        config=GatewayConfig(mode=LLMMode.LOCAL, reuse_cache=False),
    )
    req = request()
    first = gw.complete(req)
    gw.complete(req)
    assert len(calls) == 2 and first.cost_krw == Decimal(0)
    assert first.usage.input_tokens == 11 and not first.from_cache
    gw.config = GatewayConfig(mode=LLMMode.REPLAY)
    assert gw.complete(req).from_cache and len(calls) == 2


def test_local_endpoint_does_not_honor_external_proxy_or_openai_url(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.example:9000")
    p = VLLMProvider()
    assert str(p._client.base_url) == "http://127.0.0.1:8001/v1/"
    assert p._client._client.follow_redirects is False


def test_local_text_model_refuses_images_before_a_provider_call():
    p = VLLMProvider()
    gw = LLMGateway.for_provider(
        p, MemoryReplayStore(), budget=Budget(Decimal(0)), config=GatewayConfig(mode=LLMMode.LOCAL)
    )
    req = LLMRequest(
        purpose="test",
        system="test",
        messages=(LLMMessage.user(ImagePart(b"test")),),
        schema={},
        tenant_id="test",
        contains_tenant_data=True,
        prompt_id="test",
    )
    with pytest.raises(LLMError, match="text-only"):
        gw.complete(req)


def test_server_error_does_not_expose_response_body():
    def reply(req):
        return httpx.Response(400, json={"error": {"message": "private-document-body"}})

    client = openai.OpenAI(
        base_url="http://127.0.0.1:8001/v1",
        api_key="test-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(reply)),
    )
    with pytest.raises(LLMError, match="HTTP 400") as exc:
        VLLMProvider(client=client).complete(request(), timeout_s=1)
    assert "private-document-body" not in str(exc.value)
