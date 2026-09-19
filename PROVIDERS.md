# Providers

Interface (`providers/base.py`): `generate()`, `stream()`, `tool_calling()`, `context_window()`, `supports_images()`,
`supports_structured_output()`, `pricing_metadata()`, `health_check()`, plus `cost(usage)`.
Errors are normalised to `ProviderError(kind ∈ rate_limit | outage | context | auth | config | other)`; timeouts and transport failures are `outage`,
HTTP 401/403 (and 400 "API key not valid") are `auth`, 429 is `rate_limit` (with `Retry-After`), "context/too long/maximum" bodies are `context`.

| Provider | Kind | Endpoint |
|---|---|---|
| Anthropic | `anthropic` | `POST {base}/v1/messages` (`x-api-key`, `anthropic-version: 2023-06-01`) |
| OpenAI | `openai` | `POST {base}/chat/completions` (Bearer; `max_completion_tokens` on api.openai.com) |
| Qwen (DashScope, OpenAI-compatible mode) | `openai` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions` |
| DeepSeek | `openai` | `https://api.deepseek.com/v1/chat/completions` |
| Gemini | `gemini` | `POST {base}/v1beta/models/{model}:generateContent` and `:streamGenerateContent?alt=sse` (`x-goog-api-key`) |
| OpenRouter | `openrouter` | `https://openrouter.ai/api/v1/chat/completions` |
| Ollama | `ollama` | `{base}/v1/chat/completions`; health via `/api/tags`; model list via `/api/tags` |
| LM Studio | `lmstudio` | `{base}/chat/completions` (base ends in `/v1`); health via `/models` |
| any OpenAI-compatible server | `openai` + `--base-url` | `genius models add openai --name mylab --base-url http://host:8000/v1` |
| Mock | `mock` | none — deterministic, offline (default provider on first run) |

All adapters implement: single-shot `generate` (text, tool calls, JSON mode, images), SSE `stream` (text), usage incl. cached tokens, cost metadata,
error normalisation, rate-limit and authentication detection. Streaming yields text only; tool calls use `generate`.
The adapters use `httpx` against the documented REST endpoints rather than vendor SDKs (fewer dependencies, identical behaviour across vendors, trivially mockable).

## Two-stage verification (read this before worrying about a status)

| Stage | Meaning | Anthropic | Qwen | OpenAI | DeepSeek | Gemini | OpenRouter | Ollama | LM Studio | Mock |
|---|---|---|---|---|---|---|---|---|---|---|
| **Implementation** — adapter exists, request/response shapes follow the documented API | | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| **Mock tests** — 10-case matrix per adapter against mocked HTTP (success, streaming, tool call, invalid shape, malformed JSON, bad auth, 429, 5xx, timeout, context overflow) | | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| **Live authentication** — one real request succeeded with *your* credential | | NOT TESTED — KEY NOT CONFIGURED | NOT TESTED — KEY NOT CONFIGURED | NOT TESTED — KEY NOT CONFIGURED | NOT TESTED — KEY NOT CONFIGURED | NOT TESTED — KEY NOT CONFIGURED | NOT TESTED — KEY NOT CONFIGURED | NOT TESTED — no model pulled | NOT TESTED — no server | n/a (in-process, probed for real) |

A provider with no credential is **READY FOR KEY**. That is not a failure: it means the integration is built and mock-tested and only your key is missing.
Status values shown by `genius models` / the Models screen (green is only ever shown for something actually observed):

| State | Meaning |
|---|---|
| `READY FOR KEY` | cloud provider, no credential yet — never contacted |
| `NEEDS MODEL` | credential present, no model selected |
| `KEY SET · UNTESTED` | credential + model present, no live request has succeeded yet |
| `LIVE VERIFIED` ● | a real request succeeded (timestamp + latency recorded) |
| `ERROR` | the last real check failed (`auth`, `outage`, …) with the reason |
| `NOT CONFIGURED` | local provider (Ollama / LM Studio) without a model |
| `CONNECTED` ● | in-process mock or a local server answered a real health check |
| `KEY SET · NOT ADDED` | a key exists in the environment but the provider isn't added yet |

## Adding a key later (no rebuild, no config editing)

```bash
genius models key anthropic          # hidden prompt → macOS Keychain. Prints only "Configured: YES"
genius models select-model anthropic <model-id>     # or: genius models list-models anthropic
genius models test anthropic         # one tiny real request → LIVE VERIFIED (or ERROR with the reason)
genius models set-primary anthropic  # use it for every role
genius models set-fallback qwen openai              # order tried when the primary fails
```

Same for `qwen`, `openai`, `deepseek`, `gemini`, `openrouter`; local: `genius models add ollama` + `select-model ollama <tag>` + `test ollama`.
`genius models remove-key <name>` deletes the Keychain entry. Scripting: `genius models key qwen --stdin < keyfile`.
In the TUI: `^O` → select a row → `a` add key · `t` test · `m` select model · `p` set primary · `f` set fallback · `c` configure · `x` remove key.

## Configuration file (only if you want it)

`~/.genius-dev/config.toml` (global) and `.genius/config.toml` (project; overrides):

```toml
[providers.qwen]
kind = "openai"
base_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
api_key_ref = "keychain:qwen"                # keychain:<account> | env:NAME | none — a literal key is never accepted
model = "<model id>"
context_window = 128000
input_cost = 0.3                              # USD per 1M tokens (unset = cost shows $0.00)
output_cost = 1.2
cached_input_cost = 0.03                      # optional
capabilities = ["tools", "structured"]        # add "images" to enable vision routing
tier = 1                                      # 1 economy · 2 balanced · 3 strongest
local = false                                 # true = allowed in LOCAL ONLY mode
enabled = true

[general]
primary = "qwen"                              # `genius models set-primary`
[fallback]
chain = ["anthropic", "openai"]               # `genius models set-fallback`
```

Model IDs and prices are never hard-coded (only Anthropic's preset carries a default model id); set them with `select-model` / `configure --input-cost … --output-cost …`.
