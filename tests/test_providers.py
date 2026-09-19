import json

import httpx
import pytest

from genius_dev.config import ProviderConfig
from genius_dev.providers import ProviderError, build_provider
from genius_dev.providers.base import Usage


def cfg(kind, **kw):
    base = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1", "gemini": "https://generativelanguage.googleapis.com",
            "ollama": "http://localhost:11434", "lmstudio": "http://localhost:1234/v1", "openrouter": "https://openrouter.ai/api/v1"}[kind]
    return ProviderConfig(name=kind, kind=kind, base_url=base, model="m1", api_key_ref="env:TEST_KEY", input_cost=3.0, output_cost=15.0, **kw)


def transport(handler):
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test-1234567890abcdefghijkl")


async def test_openai_generate_parses_text_usage_and_tools():
    seen = {}
    def h(req):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={"model": "m1", "choices": [{"finish_reason": "stop", "message": {"content": "hello", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 40}}})
    p = build_provider(cfg("openai"), transport(h))
    c = await p.generate([{"role": "user", "content": "hi"}], "sys", tools=[{"name": "read_file", "parameters": {"type": "object"}}], json_mode=True)
    assert c.text == "hello" and c.tool_calls[0].name == "read_file" and c.tool_calls[0].args == {"path": "a.py"}
    assert (c.usage.input_tokens, c.usage.output_tokens, c.usage.cached_tokens) == (100, 20, 40)
    assert seen["url"].endswith("/chat/completions") and seen["auth"].startswith("Bearer ")
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["response_format"] == {"type": "json_object"} and "max_completion_tokens" in seen["body"]


async def test_openai_compat_local_uses_max_tokens_and_no_key_header():
    seen = {}
    def h(req):
        seen["h"], seen["b"], seen["u"] = dict(req.headers), json.loads(req.content), str(req.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    c = ProviderConfig(name="lm", kind="lmstudio", base_url="http://localhost:1234/v1", model="qwen", api_key_ref="none")
    p = build_provider(c, transport(h))
    await p.generate([{"role": "user", "content": "x"}])
    assert "authorization" not in seen["h"] and "max_tokens" in seen["b"] and seen["u"] == "http://localhost:1234/v1/chat/completions"


async def test_ollama_base_gets_v1_suffix():
    seen = {}
    def h(req):
        seen["u"] = str(req.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    p = build_provider(ProviderConfig(name="o", kind="ollama", base_url="http://localhost:11434", model="q", api_key_ref="none"), transport(h))
    await p.generate([{"role": "user", "content": "x"}])
    assert seen["u"] == "http://localhost:11434/v1/chat/completions"


async def test_anthropic_request_and_response():
    seen = {}
    def h(req):
        seen["h"], seen["b"], seen["u"] = dict(req.headers), json.loads(req.content), str(req.url)
        return httpx.Response(200, json={"model": "m1", "stop_reason": "end_turn", "content": [{"type": "text", "text": "yo"}, {"type": "tool_use", "id": "t1", "name": "tests", "input": {}}],
                                         "usage": {"input_tokens": 50, "output_tokens": 7, "cache_read_input_tokens": 10}})
    p = build_provider(cfg("anthropic", capabilities=["images"]), transport(h))
    c = await p.generate([{"role": "user", "content": "q", "images": ["QUJD"]}], "system!", tools=[{"name": "tests", "description": "d"}], json_mode=True)
    assert seen["u"] == "https://api.anthropic.com/v1/messages"
    assert seen["h"]["x-api-key"].startswith("sk-test") and seen["h"]["anthropic-version"]
    assert seen["b"]["messages"][0]["content"][0]["type"] == "image" and "JSON" in seen["b"]["system"] and seen["b"]["tools"][0]["input_schema"]
    assert c.text == "yo" and c.tool_calls[0].name == "tests" and c.usage.cached_tokens == 10 and c.usage.input_tokens == 60


async def test_gemini_request_and_response():
    seen = {}
    def h(req):
        seen["u"], seen["h"], seen["b"] = str(req.url), dict(req.headers), json.loads(req.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "hey"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 3}})
    p = build_provider(cfg("gemini"), transport(h))
    c = await p.generate([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}], "sys", json_mode=True)
    assert seen["u"].endswith("/v1beta/models/m1:generateContent") and seen["h"]["x-goog-api-key"].startswith("sk-test")
    assert seen["b"]["contents"][1]["role"] == "model" and seen["b"]["generationConfig"]["responseMimeType"] == "application/json"
    assert c.text == "hey" and c.usage.input_tokens == 9


@pytest.mark.parametrize("status,body,headers,kind", [
    (401, "bad key", {}, "auth"), (403, "nope", {}, "auth"), (429, "slow down", {"retry-after": "7"}, "rate_limit"),
    (503, "down", {}, "outage"), (400, "maximum context length exceeded", {}, "context"), (404, "no such model", {}, "config"), (418, "teapot", {}, "other"),
])
async def test_error_taxonomy(status, body, headers, kind):
    p = build_provider(cfg("openai"), transport(lambda r: httpx.Response(status, text=body, headers=headers)))
    with pytest.raises(ProviderError) as e:
        await p.generate([{"role": "user", "content": "x"}])
    assert e.value.kind == kind
    if kind == "rate_limit":
        assert e.value.retry_after == 7.0


async def test_connection_failure_is_outage():
    def h(req):
        raise httpx.ConnectError("boom")
    p = build_provider(cfg("openai"), transport(h))
    with pytest.raises(ProviderError) as e:
        await p.generate([{"role": "user", "content": "x"}])
    assert e.value.kind == "outage"


async def test_missing_key_and_model_are_reported_without_network():
    c = cfg("anthropic")
    c.api_key_ref = "env:DOES_NOT_EXIST"
    with pytest.raises(ProviderError) as e:
        await build_provider(c, transport(lambda r: pytest.fail("no request expected"))).generate([{"role": "user", "content": "x"}])
    assert e.value.kind == "auth"
    c2 = cfg("openai"); c2.model = ""
    with pytest.raises(ProviderError) as e2:
        await build_provider(c2, transport(lambda r: pytest.fail("no request"))).generate([{"role": "user", "content": "x"}])
    assert e2.value.kind == "config"


async def test_error_messages_never_leak_keys():
    p = build_provider(cfg("openai"), transport(lambda r: httpx.Response(418, text="echo sk-test-1234567890abcdefghijkl leaked")))
    with pytest.raises(ProviderError) as e:
        await p.generate([{"role": "user", "content": "x"}])
    assert "sk-test-1234567890" not in str(e.value)


async def test_openai_streaming_sse():
    sse = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n" for t in ("Hel", "lo")) + "data: [DONE]\n\n"
    p = build_provider(cfg("openai"), transport(lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})))
    assert "".join([c async for c in p.stream([{"role": "user", "content": "x"}])]) == "Hello"


async def test_health_check_reports_latency_and_failure():
    ok = build_provider(cfg("openai"), transport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})))
    good, lat, _ = await ok.health_check()
    assert good and lat >= 0
    bad = build_provider(cfg("openai"), transport(lambda r: httpx.Response(401, text="x")))
    good, _, detail = await bad.health_check()
    assert not good and "auth" in detail


def test_capabilities_and_pricing_metadata():
    p = build_provider(cfg("anthropic", capabilities=["tools", "images", "structured"], context_window=200_000))
    assert p.tool_calling() and p.supports_images() and p.supports_structured_output() and p.context_window() == 200_000
    assert p.pricing_metadata()["input_per_mtok"] == 3.0 and p.pricing_metadata()["configured"]


def test_cost_calculation_with_cache_discount():
    c = cfg("anthropic"); c.cached_input_cost = 0.3
    p = build_provider(c)
    # 1M in (400k cached), 100k out: 600k*3 + 400k*0.3 + 100k*15 = 1.8+0.12+1.5 = 3.42
    assert p.cost(Usage(1_000_000, 100_000, 400_000)) == pytest.approx(3.42)
    assert build_provider(cfg("openai")).cost(Usage(0, 0)) == 0


def test_unknown_kind_is_config_error():
    with pytest.raises(ProviderError):
        build_provider(ProviderConfig(name="x", kind="nope"))
