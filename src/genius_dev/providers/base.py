"""ModelProvider interface, shared HTTP plumbing and error taxonomy."""
from __future__ import annotations

import abc
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from ..config import ProviderConfig
from ..secrets import redact, resolve_key

Message = dict[str, Any]  # {"role": "user"|"assistant", "content": str, "images": [b64png]?}


class ProviderError(Exception):
    """kind ∈ rate_limit | outage | context | auth | config | other"""

    def __init__(self, kind: str, message: str, retry_after: float | None = None, status: int | None = None):
        super().__init__(redact(message))
        self.kind = kind
        self.retry_after = retry_after
        self.status = status


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = ""


@dataclass
class Completion:
    text: str
    usage: Usage = field(default_factory=Usage)
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    latency: float = 0.0
    finish: str = ""


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def classify_http(status: int, body: str, retry_after: float | None = None) -> ProviderError:
    low = body.lower()
    if status in (401, 403) or (status == 400 and any(k in low for k in ("api key not valid", "invalid api key", "invalid x-api-key", "incorrect api key", "api_key_invalid"))):
        return ProviderError("auth", f"authentication failed ({status})", status=status)
    if status == 429:
        return ProviderError("rate_limit", "rate limited", retry_after, status)
    if status in (400, 413) and any(s in low for s in ("context", "too long", "too many tokens", "maximum", "token limit")):
        return ProviderError("context", "context window exceeded", status=status)
    if status == 404:
        return ProviderError("config", f"endpoint or model not found: {body[:160]}", status=status)
    if status >= 500 or status == 408:
        return ProviderError("outage", f"server error {status}", retry_after, status)
    return ProviderError("other", f"HTTP {status}: {body[:200]}", status=status)


class ModelProvider(abc.ABC):
    """Contract every adapter implements.

    generate / stream / tool_calling / context_window / supports_images /
    supports_structured_output / pricing_metadata / health_check
    """

    def __init__(self, cfg: ProviderConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # -- capability surface ---------------------------------------------------
    @property
    def name(self) -> str:
        return self.cfg.name

    def tool_calling(self) -> bool:
        return "tools" in self.cfg.capabilities

    def context_window(self) -> int:
        return self.cfg.context_window

    def supports_images(self) -> bool:
        return "images" in self.cfg.capabilities

    def supports_structured_output(self) -> bool:
        return "structured" in self.cfg.capabilities

    def pricing_metadata(self) -> dict[str, Any]:
        return {"input_per_mtok": self.cfg.input_cost, "output_per_mtok": self.cfg.output_cost,
                "cached_input_per_mtok": self.cfg.cached_input_cost, "configured": bool(self.cfg.input_cost or self.cfg.output_cost)}

    def cost(self, u: Usage) -> float:
        c = self.cfg
        cached_rate = c.cached_input_cost if c.cached_input_cost is not None else c.input_cost
        fresh = max(0, u.input_tokens - u.cached_tokens)
        return (fresh * c.input_cost + u.cached_tokens * cached_rate + u.output_tokens * c.output_cost) / 1_000_000

    # -- transport ------------------------------------------------------------
    def _key(self) -> str | None:
        return resolve_key(self.cfg.api_key_ref)

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), transport=self._transport)
        return self._client

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def headers(self) -> dict[str, str]:
        return {"content-type": "application/json"}

    async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self.client().post(url, headers=self.headers(), json=body)
        except httpx.TimeoutException as e:
            raise ProviderError("outage", f"request timed out ({type(e).__name__})") from e
        except httpx.TransportError as e:
            raise ProviderError("outage", f"cannot reach {self.cfg.base_url}: {type(e).__name__}") from e
        if r.status_code >= 400:
            ra = r.headers.get("retry-after")
            raise classify_http(r.status_code, r.text, float(ra) if ra and ra.replace(".", "").isdigit() else None)
        try:
            data = r.json()
        except json.JSONDecodeError as e:
            raise ProviderError("other", "malformed JSON from provider") from e
        if not isinstance(data, dict):
            raise ProviderError("other", "unexpected response shape (not an object)")
        return data

    async def _stream_sse(self, url: str, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self.client().stream("POST", url, headers=self.headers(), json=body) as r:
                if r.status_code >= 400:
                    text = (await r.aread()).decode(errors="replace")
                    raise classify_http(r.status_code, text)
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload in ("", "[DONE]"):
                        continue
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        except httpx.TimeoutException as e:
            raise ProviderError("outage", f"stream timed out ({type(e).__name__})") from e
        except httpx.TransportError as e:
            raise ProviderError("outage", f"stream failed: {type(e).__name__}") from e

    # -- to implement ---------------------------------------------------------
    @abc.abstractmethod
    async def generate(self, messages: list[Message], system: str = "", tools: list[dict] | None = None,
                       json_mode: bool = False, max_tokens: int = 4096, temperature: float | None = None) -> Completion: ...

    async def stream(self, messages: list[Message], system: str = "", tools: list[dict] | None = None, json_mode: bool = False,
                     max_tokens: int = 4096, temperature: float | None = None) -> AsyncIterator[str]:
        """Default: non-streaming fallback yields the whole text once. Streams text only (tool calls use generate())."""
        c = await self.generate(messages, system, tools, json_mode, max_tokens, temperature)
        yield c.text

    async def health_check(self) -> tuple[bool, float, str]:
        """Return (ok, latency_seconds, detail)."""
        t = time.perf_counter()
        try:
            await self.generate([{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=16)
            return True, time.perf_counter() - t, "ok"
        except ProviderError as e:
            return False, time.perf_counter() - t, f"{e.kind}: {e}"
