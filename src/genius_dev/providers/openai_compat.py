"""OpenAI-compatible chat-completions adapter (OpenAI, Qwen/DashScope, DeepSeek, OpenRouter, Ollama, LM Studio, vLLM...)."""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from .base import Completion, Message, ModelProvider, ProviderError, ToolCall, Usage


class OpenAICompatProvider(ModelProvider):
    def _base(self) -> str:
        b = self.cfg.base_url.rstrip("/")
        if self.cfg.kind == "ollama" and not b.endswith("/v1"):
            b += "/v1"           # Ollama exposes an OpenAI-compatible surface under /v1
        return b

    def headers(self) -> dict[str, str]:
        h = super().headers()
        key = self._key()
        if key:
            h["authorization"] = f"Bearer {key}"
        if self.cfg.kind == "openrouter":
            h["x-title"] = "Genius Dev"
        return h

    def _payload(self, messages: list[Message], system: str, tools, json_mode, max_tokens, temperature, stream=False) -> dict:
        msgs: list[dict[str, Any]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        for m in messages:
            if m.get("images"):
                parts: list[dict[str, Any]] = [{"type": "text", "text": m["content"]}]
                parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in m["images"]]
                msgs.append({"role": m["role"], "content": parts})
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        body: dict[str, Any] = {"model": self.cfg.model, "messages": msgs}
        # api.openai.com's newer models require max_completion_tokens; most compat servers accept max_tokens.
        body["max_completion_tokens" if "api.openai.com" in self.cfg.base_url else "max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                                "parameters": t.get("parameters", {"type": "object", "properties": {}})}}
                             for t in tools]
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    @staticmethod
    def _usage(u: dict[str, Any] | None) -> Usage:
        u = u or {}
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        return Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), cached)

    async def generate(self, messages, system="", tools=None, json_mode=False, max_tokens=4096, temperature=None) -> Completion:
        if not self.cfg.model:
            raise ProviderError("config", f"provider '{self.name}' has no model configured")
        t = time.perf_counter()
        data = await self._post(f"{self._base()}/chat/completions",
                                self._payload(messages, system, tools, json_mode, max_tokens, temperature))
        try:
            ch = data["choices"][0]
            msg = ch["message"]
        except (KeyError, IndexError) as e:
            raise ProviderError("other", "unexpected response shape") from e
        calls = []
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc["function"]["name"], args, tc.get("id", "")))
        return Completion(msg.get("content") or "", self._usage(data.get("usage")), calls,
                          data.get("model", self.cfg.model), time.perf_counter() - t, ch.get("finish_reason") or "")

    async def stream(self, messages, system="", tools=None, json_mode=False, max_tokens=4096, temperature=None) -> AsyncIterator[str]:
        body = self._payload(messages, system, tools, json_mode, max_tokens, temperature, stream=True)
        async for chunk in self._stream_sse(f"{self._base()}/chat/completions", body):
            for ch in chunk.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content")
                if piece:
                    yield piece

    async def health_check(self) -> tuple[bool, float, str]:
        if self.cfg.kind in ("ollama", "lmstudio"):
            t = time.perf_counter()
            url = f"{self.cfg.base_url.rstrip('/')}/api/tags" if self.cfg.kind == "ollama" else f"{self._base()}/models"
            try:
                r = await self.client().get(url, headers=self.headers())
                ok = r.status_code < 400
                return ok, time.perf_counter() - t, "ok" if ok else f"HTTP {r.status_code}"
            except Exception as e:  # noqa: BLE001
                return False, time.perf_counter() - t, f"unreachable ({type(e).__name__})"
        return await super().health_check()

    async def list_models(self) -> list[str]:
        try:
            if self.cfg.kind == "ollama":
                r = await self.client().get(f"{self.cfg.base_url.rstrip('/')}/api/tags")
                return [m["name"] for m in r.json().get("models", [])]
            r = await self.client().get(f"{self._base()}/models", headers=self.headers())
            return [m["id"] for m in r.json().get("data", [])]
        except Exception:  # noqa: BLE001
            return []
