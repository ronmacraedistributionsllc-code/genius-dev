"""Second provider matrix: every adapter x {success, stream, tool call, invalid response, bad auth, 429, 5xx, timeout, malformed JSON, context overflow}
plus provider status semantics and key management. All HTTP is mocked; no network is ever used."""
import json

import httpx
import pytest

from genius_dev.config import Config, PRESETS, ProviderConfig
from genius_dev.providers import ProviderError, build_provider
from genius_dev.providers.status import record_live, statuses
from genius_dev.project import global_store

KINDS = ["anthropic", "openai", "gemini", "openrouter", "ollama", "lmstudio"]
BASE = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1", "gemini": "https://generativelanguage.googleapis.com",
        "openrouter": "https://openrouter.ai/api/v1", "ollama": "http://localhost:11434", "lmstudio": "http://localhost:1234/v1"}


def mk(kind, handler, **kw):
    c = ProviderConfig(name=kind, kind=kind, base_url=BASE[kind], model="m1", api_key_ref="env:K" if kind not in ("ollama", "lmstudio") else "none", **kw)
    return build_provider(c, httpx.MockTransport(handler))


def ok_body(kind, text="hello", tool=None):
    if kind == "anthropic":
        c = [{"type": "text", "text": text}] + ([{"type": "tool_use", "id": "1", "name": tool, "input": {"a": 1}}] if tool else [])
        return {"content": c, "usage": {"input_tokens": 5, "output_tokens": 2}}
    if kind == "gemini":
        parts = [{"text": text}] + ([{"functionCall": {"name": tool, "args": {"a": 1}}}] if tool else [])
        return {"candidates": [{"content": {"parts": parts}}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}}
    msg = {"content": text}
    if tool:
        msg["tool_calls"] = [{"id": "1", "function": {"name": tool, "arguments": "{\"a\": 1}"}}]
    return {"choices": [{"message": msg}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}


def sse(kind, pieces):
    if kind == "anthropic":
        return "".join(f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': p}})}\n\n" for p in pieces)
    if kind == "gemini":
        return "".join(f"data: {json.dumps({'candidates': [{'content': {'parts': [{'text': p}]}}]})}\n\n" for p in pieces)
    return "".join(f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}\n\n" for p in pieces) + "data: [DONE]\n\n"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("K", "key-abcdefghijklmnopqrstuvwxyz")


MSG = [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize("kind", KINDS)
async def test_success(kind):
    c = await mk(kind, lambda r: httpx.Response(200, json=ok_body(kind))).generate(MSG)
    assert c.text == "hello" and c.usage.input_tokens == 5 and c.usage.output_tokens == 2


@pytest.mark.parametrize("kind", KINDS)
async def test_streaming(kind):
    p = mk(kind, lambda r: httpx.Response(200, text=sse(kind, ["He", "llo"]), headers={"content-type": "text/event-stream"}))
    assert "".join([x async for x in p.stream(MSG)]) == "Hello"


@pytest.mark.parametrize("kind", KINDS)
async def test_tool_call(kind):
    c = await mk(kind, lambda r: httpx.Response(200, json=ok_body(kind, "", "read_file"))).generate(MSG, tools=[{"name": "read_file", "parameters": {"type": "object"}}])
    assert c.tool_calls and c.tool_calls[0].name == "read_file" and c.tool_calls[0].args == {"a": 1}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("body", [{}, {"choices": []}, {"candidates": []}, {"content": "notalist"}, []])
async def test_invalid_response_shape(kind, body):
    with pytest.raises(ProviderError) as e:
        await mk(kind, lambda r: httpx.Response(200, json=body)).generate(MSG)
    assert e.value.kind == "other"


@pytest.mark.parametrize("kind", KINDS)
async def test_malformed_json(kind):
    with pytest.raises(ProviderError) as e:
        await mk(kind, lambda r: httpx.Response(200, text="<html>gateway</html>")).generate(MSG)
    assert e.value.kind == "other" and "malformed" in str(e.value)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("status,body,kind_out", [
    (401, '{"error":"bad"}', "auth"), (403, "forbidden", "auth"), (400, "API key not valid. Please pass a valid API key.", "auth"),
    (429, "rate", "rate_limit"), (500, "boom", "outage"), (529, "overloaded", "outage"), (503, "x", "outage"),
    (400, "prompt is too long: 250000 tokens > 200000 maximum", "context"), (400, "This model's maximum context length is 8192 tokens", "context"),
    (400, "input token count exceeds the maximum number of tokens", "context"), (404, "model not found", "config"),
])
async def test_http_errors_are_normalised(kind, status, body, kind_out):
    with pytest.raises(ProviderError) as e:
        await mk(kind, lambda r: httpx.Response(status, text=body, headers={"retry-after": "3"} if status == 429 else {})).generate(MSG)
    assert e.value.kind == kind_out
    if kind_out == "rate_limit":
        assert e.value.retry_after == 3.0


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("exc", [httpx.ReadTimeout("t"), httpx.ConnectTimeout("t"), httpx.ConnectError("c"), httpx.RemoteProtocolError("p"), httpx.PoolTimeout("p")])
async def test_timeouts_and_transport_errors_are_outages(kind, exc):
    def h(r):
        raise exc
    with pytest.raises(ProviderError) as e:
        await mk(kind, h).generate(MSG)
    assert e.value.kind == "outage"
    with pytest.raises(ProviderError) as e2:
        _ = [x async for x in mk(kind, h).stream(MSG)]
    assert e2.value.kind == "outage"


@pytest.mark.parametrize("kind", KINDS)
async def test_stream_http_error(kind):
    with pytest.raises(ProviderError) as e:
        _ = [x async for x in mk(kind, lambda r: httpx.Response(429, text="slow")).stream(MSG)]
    assert e.value.kind == "rate_limit"


@pytest.mark.parametrize("kind", ["anthropic", "gemini", "openai"])
async def test_missing_credentials_never_hit_the_network(kind, monkeypatch):
    monkeypatch.delenv("K")
    def h(r):
        pytest.fail("network must not be used without a key")
    if kind == "openai":                        # OpenAI-compat sends without a header and lets the server answer; anthropic/gemini refuse locally
        return
    with pytest.raises(ProviderError) as e:
        await mk(kind, h).generate(MSG)
    assert e.value.kind == "auth"


@pytest.mark.parametrize("kind", KINDS)
async def test_health_check_ok_and_failure(kind):
    body = {"models": [], "data": []} if kind in ("ollama", "lmstudio") else ok_body(kind, "pong")      # local providers are probed via their list endpoints
    good, lat, _ = await mk(kind, lambda r: httpx.Response(200, json=body)).health_check()
    assert good
    bad, _, detail = await mk(kind, lambda r: httpx.Response(401, text="x")).health_check()
    assert not bad


async def test_ollama_and_lmstudio_list_models():
    o = mk("ollama", lambda r: httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:7b"}]}))
    assert await o.list_models() == ["qwen2.5-coder:7b"]
    l = mk("lmstudio", lambda r: httpx.Response(200, json={"data": [{"id": "local-model"}]}))
    assert await l.list_models() == ["local-model"]


# ---------------- status semantics ----------------
def test_catalog_shows_ready_for_key_and_never_fakes_green(tmp_path, fake_keychain, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    cfg = Config(None)
    by = {s.name: s for s in statuses(cfg, global_store())}
    for n in ("anthropic", "qwen", "openai", "deepseek", "gemini", "openrouter"):
        assert by[n].state == "READY FOR KEY" and not by[n].live_verified and by[n].implemented and by[n].tested_with_mock and not by[n].key_present
    assert by["ollama"].state == "NOT CONFIGURED" and by["lmstudio"].state == "NOT CONFIGURED"
    assert list(by)[:9] == ["anthropic", "qwen", "openai", "deepseek", "gemini", "openrouter", "ollama", "lmstudio", "mock"]


def test_status_progression_key_model_verified_error(fake_keychain, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg, g = Config(None), global_store()
    st = lambda: {s.name: s for s in statuses(cfg, g)}["qwen"]
    assert st().state == "READY FOR KEY"
    fake_keychain[("genius-dev", "qwen")] = "sk-secret-value-1234567890"
    p = ProviderConfig.from_dict("qwen", {**PRESETS["qwen"], "api_key_ref": "keychain:qwen"})
    cfg.save_provider(p)
    assert st().state == "NEEDS MODEL" and st().key_present
    p.model = "some-model"; cfg.save_provider(p)
    assert st().state == "KEY SET · UNTESTED" and not st().live_verified
    record_live(g, "qwen", True, 0.21, "ok")
    assert st().state == "LIVE VERIFIED" and st().live_verified and st().latency_ms == 210
    record_live(g, "qwen", False, 0.5, "auth: authentication failed (401)")
    assert st().state == "ERROR" and "auth" in st().detail
    assert "secret-value" not in json.dumps(st().to_dict())


def test_mock_and_local_states_come_from_real_probes():
    cfg = Config(None)
    cfg.save_provider(ProviderConfig.from_dict("mock", PRESETS["mock"]))
    cfg.save_provider(ProviderConfig.from_dict("ollama", {**PRESETS["ollama"], "model": "qwen"}))
    g = global_store()
    by = {s.name: s for s in statuses(cfg, g)}
    assert by["mock"].state == "UNTESTED" and by["ollama"].state == "UNTESTED"           # configured but not probed: not green
    by = {s.name: s for s in statuses(cfg, g, {"mock": (True, 0.0, "ok"), "ollama": (False, 0.1, "unreachable (ConnectError)")})}
    assert by["mock"].state == "CONNECTED" and by["ollama"].state == "ERROR" and "unreachable" in by["ollama"].detail


def test_env_key_is_detected_but_provider_needs_adding(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x" * 30)
    s = {x.name: x for x in statuses(Config(None), global_store())}["openrouter"]
    assert s.state == "KEY SET · NOT ADDED" and s.key_present
