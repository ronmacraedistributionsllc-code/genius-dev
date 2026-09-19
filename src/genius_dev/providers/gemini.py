"""Google Gemini (generateContent) adapter."""
from __future__ import annotations

import time
from typing import Any, AsyncIterator

from .base import Completion, ModelProvider, ProviderError, ToolCall, Usage


class GeminiProvider(ModelProvider):
    def headers(self) -> dict[str, str]:
        h = super().headers()
        key = self._key()
        if key:
            h["x-goog-api-key"] = key
        return h

    def _payload(self, messages, system, tools, json_mode, max_tokens, temperature) -> dict:
        contents = []
        for m in messages:
            parts: list[dict[str, Any]] = [{"text": m["content"]}]
            for b in m.get("images") or []:
                parts.append({"inline_data": {"mime_type": "image/png", "data": b}})
            contents.append({"role": "model" if m["role"] == "assistant" else "user", "parts": parts})
        gen: dict[str, Any] = {"maxOutputTokens": max_tokens}
        if temperature is not None:
            gen["temperature"] = temperature
        if json_mode:
            gen["responseMimeType"] = "application/json"
        body: dict[str, Any] = {"contents": contents, "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {"type": "object", "properties": {}})}
                for t in tools]}]
        return body

    def _url(self, method: str) -> str:
        return f"{self.cfg.base_url.rstrip('/')}/v1beta/models/{self.cfg.model}:{method}"

    async def generate(self, messages, system="", tools=None, json_mode=False, max_tokens=4096, temperature=None) -> Completion:
        if not self.cfg.model:
            raise ProviderError("config", f"provider '{self.name}' has no model configured")
        if not self._key():
            raise ProviderError("auth", f"no API key for '{self.name}' ({self.cfg.api_key_ref})")
        t = time.perf_counter()
        data = await self._post(self._url("generateContent"), self._payload(messages, system, tools, json_mode, max_tokens, temperature))
        cands = data.get("candidates") or []
        if not cands:
            raise ProviderError("other", f"no candidates returned ({data.get('promptFeedback', {})})")
        text, calls = "", []
        for p in (cands[0].get("content") or {}).get("parts", []):
            if "text" in p:
                text += p["text"]
            elif "functionCall" in p:
                calls.append(ToolCall(p["functionCall"]["name"], p["functionCall"].get("args", {})))
        u = data.get("usageMetadata", {})
        usage = Usage(u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0), u.get("cachedContentTokenCount", 0))
        return Completion(text, usage, calls, self.cfg.model, time.perf_counter() - t, cands[0].get("finishReason", ""))

    async def stream(self, messages, system="", tools=None, json_mode=False, max_tokens=4096, temperature=None) -> AsyncIterator[str]:
        body = self._payload(messages, system, tools, json_mode, max_tokens, temperature)
        async for ev in self._stream_sse(self._url("streamGenerateContent") + "?alt=sse", body):
            for c in ev.get("candidates") or []:
                for p in (c.get("content") or {}).get("parts", []):
                    if p.get("text"):
                        yield p["text"]
