"""LLM gateway: replay/fake provider, budget reservation/refusal, retry, schema, consent."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from la_helpers import gateway

from jettae.llm import (
    Budget,
    BudgetExceeded,
    ExternalLLMNotAllowed,
    FakeProvider,
    FileReplayStore,
    FileTenantPolicy,
    ImagePart,
    LiveCallRefused,
    LLMMessage,
    LLMMode,
    LLMRefusal,
    LLMRequest,
    MemoryReplayStore,
    ModelPrice,
    PriceTable,
    ProviderResponse,
    ReplayMiss,
    SchemaValidationError,
    StaticTenantPolicy,
    TokenUsage,
    TransientLLMError,
    gateway_from_env,
    strict_object,
)
from jettae.llm.budget import estimate_max_input_tokens, max_cost_krw

SCHEMA = strict_object({"answer": {"type": "string"}, "n": {"type": "integer"}})


def req(**kw) -> LLMRequest:
    base = dict(
        purpose="test",
        system="sys",
        messages=(LLMMessage.user("hello 안녕"),),
        schema=SCHEMA,
        tenant_id="t1",
        contains_tenant_data=True,
        prompt_id="p",
    )
    base.update(kw)
    return LLMRequest(**base)


def test_offline_is_default_and_never_builds_a_provider(tmp_path: Path):
    gw = gateway_from_env({"JETTAE_LLM_CACHE_DIR": str(tmp_path)})
    assert gw.config.mode is LLMMode.OFFLINE and gw.provider is None
    assert gw.model == "claude-opus-5-5"
    with pytest.raises(ReplayMiss):
        gw.complete(req())


def test_live_mode_requires_positive_budget(tmp_path: Path):
    with pytest.raises(LiveCallRefused):
        gateway_from_env({"JETTAE_LLM_MODE": "live", "JETTAE_LLM_CACHE_DIR": str(tmp_path)})
    with pytest.raises(LiveCallRefused):
        gateway_from_env(
            {
                "JETTAE_LLM_MODE": "live",
                "JETTAE_LLM_BUDGET_KRW": "0",
                "JETTAE_LLM_CACHE_DIR": str(tmp_path),
            }
        )


def test_record_then_replay_same_answer_without_provider(tmp_path: Path):
    store = FileReplayStore(tmp_path)
    fake = FakeProvider([{"answer": "가", "n": 3}])
    live = gateway(fake, store=store)
    r1 = live.complete(req())
    assert r1.data == {"answer": "가", "n": 3} and not r1.from_cache
    assert r1.cost_krw is not None and r1.cost_krw > 0
    # replay: no provider at all, same key -> same data, zero cost
    replay = gateway(None, mode=LLMMode.REPLAY, store=store, model=fake.model)
    r2 = replay.complete(req())
    assert r2.from_cache and r2.data == r1.data and r2.cost_krw == 0
    assert r2.replay_key == r1.replay_key
    # live mode also reuses the recorded answer (no second provider call)
    assert live.complete(req()).from_cache and len(fake.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"prompt_version": "2"},
        {"tool_versions": {"calculate_due": "2"}},
        {"as_of": date(2025, 11, 2)},
        {"tenant_id": "t2"},
        {"system": "other system prompt"},
        {"messages": (LLMMessage.user("hello 안녕!"),)},
    ],
)
def test_replay_key_covers_tenant_asof_input_prompt_tools(change):
    store = MemoryReplayStore()
    gateway(FakeProvider([{"answer": "x", "n": 1}]), store=store).complete(req())
    replay = gateway(None, mode=LLMMode.REPLAY, store=store, model="fake-model-1")
    replay.complete(req())  # same request -> hit
    with pytest.raises(ReplayMiss):
        replay.complete(req(**change))
    with pytest.raises(ReplayMiss):  # another model never reuses the answer
        gateway(None, mode=LLMMode.REPLAY, store=store, model="other-model").complete(req())


def test_image_hash_is_part_of_the_key():
    a = req(messages=(LLMMessage.user(ImagePart(b"img-a"), "t"),))
    b = req(messages=(LLMMessage.user(ImagePart(b"img-b"), "t"),))
    assert a.input_hash != b.input_hash and a.replay_key("m") != b.replay_key("m")


def test_budget_reserve_then_settle_actual_cost(tmp_path: Path):
    fake = FakeProvider([{"answer": "ok", "n": 1}])
    gw = gateway(fake, budget_krw="100")
    price = gw.prices.get(fake.model)
    reserved = max_cost_krw(req(), price)
    seen: dict[str, Decimal] = {}

    def spy(request, *, timeout_s):  # reservation is in place while the provider runs
        seen["reserved"] = gw.budget.reserved_krw
        return FakeProvider.complete(fake, request, timeout_s=timeout_s)

    fake.complete = spy  # type: ignore[method-assign]
    res = gw.complete(req())
    assert seen["reserved"] == reserved.quantize(Decimal("0.0001"), rounding="ROUND_CEILING")
    assert gw.budget.reserved_krw == 0
    assert gw.budget.spent_krw == res.cost_krw == price.cost(res.usage)
    assert res.cost_krw < reserved  # the reservation is an upper bound


def test_budget_refusal_before_any_call():
    fake = FakeProvider([{"answer": "never", "n": 0}])
    gw = gateway(fake, budget_krw="0.5")
    with pytest.raises(BudgetExceeded):
        gw.complete(req(max_output_tokens=4096))
    assert fake.calls == [] and gw.budget.spent_krw == 0 and gw.budget.reserved_krw == 0


def test_budget_ledger_is_cumulative(tmp_path: Path):
    ledger = tmp_path / "ledger.jsonl"
    b = Budget(Decimal("10"), ledger_path=ledger)
    r = b.reserve(Decimal("3"), model="m", purpose="x")
    b.settle(r, Decimal("2.5"))
    again = Budget(Decimal("10"), ledger_path=ledger)
    assert again.spent_krw == Decimal("2.5") and again.remaining_krw == Decimal("7.5")
    with pytest.raises(BudgetExceeded):
        again.reserve(Decimal("8"), model="m", purpose="x")
    with pytest.raises(RuntimeError):
        b.settle(r, Decimal("1"))


def test_unknown_price_is_refused_unless_explicitly_allowed():
    fake = FakeProvider([{"answer": "a", "n": 1}, {"answer": "a", "n": 1}])
    with pytest.raises(LiveCallRefused, match="unknown"):
        gateway(fake, price=None).complete(req())
    assert fake.calls == []
    res = gateway(fake, price=None, allow_unknown_price=True).complete(req())
    assert res.cost_krw is None and res.data["n"] == 1


def test_price_table_from_env_needs_exchange_rate():
    assert PriceTable.from_env({}).get("claude-opus-5-5") is None  # no FX rate assumed
    p = PriceTable.from_env({"JETTAE_LLM_KRW_PER_USD": "1400"}).get("claude-opus-5-5")
    assert p is not None and p.input_krw_per_mtok == Decimal("5600")
    env = {
        "JETTAE_LLM_PRICES": json.dumps(
            {"x": {"input_krw_per_mtok": "1", "output_krw_per_mtok": "2"}}
        )
    }
    assert PriceTable.from_env(env).get("x") == ModelPrice(
        Decimal(1), Decimal(2), "JETTAE_LLM_PRICES"
    )
    assert PriceTable.from_env({}).get("gpt-whatever") is None


def test_transient_errors_retry_with_backoff_and_limit():
    sleeps: list[float] = []
    fake = FakeProvider(
        [{"answer": "ok", "n": 1}],
        failures={1: TransientLLMError("429"), 2: TransientLLMError("529", maybe_billed=True)},
    )
    gw = gateway(fake, sleep=sleeps.append, max_retries=2, timeout_s=7.0)
    res = gw.complete(req())
    assert res.attempts == 3 and sleeps == [0.5, 1.0]
    assert fake.timeouts == [7.0, 7.0, 7.0]
    # maybe-billed failure keeps its reservation as spent; the 429 is not charged
    assert (
        gw.budget.spent_krw
        == max_cost_krw(req(), gw.prices.get(fake.model)).quantize(
            Decimal("0.0001"), rounding="ROUND_CEILING"
        )
        + res.cost_krw
    )
    fake2 = FakeProvider([], failures={i: TransientLLMError("down") for i in range(1, 5)})
    with pytest.raises(TransientLLMError):
        gateway(fake2, max_retries=2).complete(req())
    assert len(fake2.timeouts) == 3  # 1 + 2 retries, no more


def test_schema_validation_one_retry_then_error():
    fake = FakeProvider(['{"answer": 5}', {"answer": "ok", "n": 2}])
    res = gateway(fake).complete(req())
    assert res.data == {"answer": "ok", "n": 2} and len(fake.calls) == 2
    bad = FakeProvider(["not json", {"answer": "x"}])
    with pytest.raises(SchemaValidationError):
        gateway(bad).complete(req())
    store = MemoryReplayStore()
    with pytest.raises(SchemaValidationError):
        gateway(FakeProvider(["{}", "{}"]), store=store).complete(req())
    assert len(store) == 0  # invalid output is never recorded


class _StopProvider:
    name, model, is_external, supports_vision = "stub", "fake-model-1", False, True

    def __init__(self, stop: str) -> None:
        self.stop = stop

    def complete(self, request, *, timeout_s):
        return ProviderResponse("{}", TokenUsage(10, 20), self.model, self.stop)


@pytest.mark.parametrize("stop,exc", [("refusal", LLMRefusal), ("max_tokens", Exception)])
def test_refusal_and_truncation_are_errors_but_cost_is_settled(stop, exc):
    gw = gateway(_StopProvider(stop))
    with pytest.raises(exc):
        gw.complete(req())
    assert gw.budget.reserved_krw == 0 and gw.budget.spent_krw > 0
    assert gw.records[-1].usage.output_tokens == 20


def test_tenant_data_needs_allow_external_llm(tmp_path: Path):
    ext = FakeProvider([{"answer": "a", "n": 1}] * 3, is_external=True)
    gw = gateway(ext)
    with pytest.raises(ExternalLLMNotAllowed):
        gw.complete(req())
    assert ext.calls == []
    # public data (no tenant data) may go to an external provider
    gw.complete(req(contains_tenant_data=False, tenant_id="public"))
    gw.tenant_policy = StaticTenantPolicy(frozenset({"t1"}))
    gw.complete(req())
    assert len(ext.calls) == 2
    # file policy: only an explicit true counts
    f = tmp_path / "tenants.json"
    f.write_text(
        json.dumps({"t1": {"allow_external_llm": True}, "t2": {"allow_external_llm": "yes"}})
    )
    pol = FileTenantPolicy(f)
    assert pol.allow_external_llm("t1") and not pol.allow_external_llm("t2")
    assert not FileTenantPolicy(None).allow_external_llm("t1")


def test_replay_store_is_tenant_scoped_and_validates_keys(tmp_path: Path):
    store = FileReplayStore(tmp_path)
    key = "a" * 64
    store.put("t1", key, {"key": key, "tenant_id": "t1", "raw_text": "{}"})
    assert store.get("t1", key) is not None and store.get("t2", key) is None
    with pytest.raises(ValueError):
        store.get("t1", "../../etc/passwd")


def test_reservation_estimate_is_an_upper_bound_of_fake_usage():
    r = req(messages=(LLMMessage.user("가" * 500, ImagePart(b"x")),))
    usage = FakeProvider([{"answer": "a", "n": 1}]).complete(r, timeout_s=1).usage
    assert estimate_max_input_tokens(r) >= usage.input_tokens


def test_anthropic_provider_request_shape_and_usage():
    from jettae.llm.anthropic import AnthropicProvider

    captured = {}

    class Msgs:
        def create(self, **kw):
            captured.update(kw)
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[
                    SimpleNamespace(type="thinking", thinking=""),
                    SimpleNamespace(type="text", text='{"answer": "a", "n": 1}'),
                ],
                usage=SimpleNamespace(
                    input_tokens=11,
                    output_tokens=7,
                    cache_read_input_tokens=None,
                    cache_creation_input_tokens=3,
                ),
                model="claude-opus-5-5",
                _request_id="req_1",
            )

    p = AnthropicProvider("claude-opus-5-5", client=SimpleNamespace(messages=Msgs()))
    r = req(messages=(LLMMessage.user(ImagePart(b"\x89PNG", "image/png"), "읽어 주세요"),))
    out = p.complete(r, timeout_s=9)
    assert captured["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert captured["timeout"] == 9 and captured["max_tokens"] == r.max_output_tokens
    blocks = captured["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["type"] == "base64"
    assert out.raw_text == '{"answer": "a", "n": 1}'
    assert out.usage == TokenUsage(11, 7, 0, 3) and out.request_id == "req_1"


def test_openai_provider_request_shape_and_refusal(monkeypatch):
    monkeypatch.delenv("JETTAE_OPENAI_MODEL", raising=False)
    from jettae.llm.openai import OpenAIProvider

    captured = {}

    def create(**kw):
        captured.update(kw)
        msg = SimpleNamespace(content=None, refusal="no")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2),
            model="m-1",
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    p = OpenAIProvider("m-1", client=client)
    out = p.complete(req(messages=(LLMMessage.user(ImagePart(b"x")),)), timeout_s=3)
    assert captured["response_format"]["json_schema"]["strict"] is True
    assert captured["messages"][0] == {"role": "system", "content": "sys"}
    assert captured["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/png")
    assert out.stop_reason == "refusal" and out.usage == TokenUsage(5, 2)
    with pytest.raises(Exception, match="JETTAE_OPENAI_MODEL"):
        OpenAIProvider(None, client=client)
