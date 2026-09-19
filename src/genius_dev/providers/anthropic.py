"""Anthropic Messages API adapter."""
from __future__ import annotations

import time
from typing import Any, AsyncIterator

from .base import Completion, ModelProvider, ProviderError, ToolCall, Usage

API_VERSION = "2023-06-01"


class AnthropicProvider(ModelProvider):
    def headers(self) -> dict[str, str]:
        h = super().headers()
        h["anthropic-version"] = API_VERSION
        key = self._key()
        if key:
            h["x-api-key"] = key
        return h

    def _payload(self, messages, system, tools, json_mode, max_tokens, temperature, stream=False) -> dict:
        msgs: list[dict[str, Any]] = []
        for m in messages:
            if m.get("images"):
                blocks: list[dict[str, Any]] = [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b}} for b in m["images"]]
                blocks.append({"type": "text", "text": m["content"]})
                msgs.append({"role": m["role"], "content": blocks})
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        sys = system + ("\n\nRespond with a single valid JSON object and nothing else." if json_mode else "")
        body: dict[str, Any] = {"model": self.cfg.model, "max_tokens": max_tokens, "messages": msgs}
        if sys:
            body["system"] = sys
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                              "input_schema": t.get("parameters", {"type": "object", "properties": {}})} for t in tools]
        if stream:
            body["stream"] = True
        return body

    async def generate(self, messages, system="", tools=None, json_mode=False, max_tokens=4096, temperature=None) -> Completion:
        if not self.cfg.model:
            raise ProviderError("config", f"provider '{self.name}' has no model configured")
        if not self._key():
            raise ProviderError("auth", f"no API key for '{self.name}' ({self.cfg.api_key_ref})")
        t = time.perf_counter()
        data = await self._post(f"{self.cfg.base_url.rstrip('/')}/v1/messages",
                                self._payload(messages, system, tools, json_mode, max_tokens, temperature))
        if not isinstance(data.get("content"), list):
            raise ProviderError("other", "unexpected response shape (no content blocks)")
        text, calls = "", []
        for b in data.get("content", []):
            if b.get("type") == "text":
                text += b.get("text", "")
            elif b.get("type") == "tool_use":
                calls.append(ToolCall(b["name"], b.get("input", {}), b.get("id", "")))
        u = data.get("usage", {})
        usage = Usage((u.get("input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0),
                      u.get("output_tokens", 0) or 0, u.get("cache_read_input_tokens", 0) or 0)
        return Completion(text, usage, calls, data.get("model", self.cfg.model), time.perf_counter() - t, data.get("stop_reason") or "")

    async def stream(self, messages, system="", tools=None, json_mode=False, max_tokens=4096, temperature=None) -> AsyncIterator[str]:
        body = self._payload(messages, system, tools, json_mode, max_tokens, temperature, stream=True)
        async for ev in self._stream_sse(f"{self.cfg.base_url.rstrip('/')}/v1/messages", body):
            if ev.get("type") == "content_block_delta":
                piece = (ev.get("delta") or {}).get("text")
                if piece:
                    yield piece
