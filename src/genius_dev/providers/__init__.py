from __future__ import annotations

from typing import Any

import httpx

from ..config import ProviderConfig
from .anthropic import AnthropicProvider
from .base import Completion, Message, ModelProvider, ProviderError, ToolCall, Usage, estimate_tokens
from .gemini import GeminiProvider
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider

KINDS: dict[str, Any] = {
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "mock": MockProvider,
    "openai": OpenAICompatProvider,
    "openrouter": OpenAICompatProvider,
    "ollama": OpenAICompatProvider,
    "lmstudio": OpenAICompatProvider,
}


def build_provider(cfg: ProviderConfig, transport: httpx.AsyncBaseTransport | None = None) -> ModelProvider:
    try:
        cls = KINDS[cfg.kind]
    except KeyError:
        raise ProviderError("config", f"unknown provider kind '{cfg.kind}'") from None
    return cls(cfg, transport=transport)


__all__ = ["build_provider", "ModelProvider", "ProviderError", "Completion", "Usage", "ToolCall", "Message", "estimate_tokens",
           "MockProvider", "KINDS"]
