# Genius Dev

A local-first autonomous software-engineering CLI/TUI for macOS. You describe software in plain language; Genius Dev turns it into explicit **requirements**,
plans it, checkpoints, edits code, runs tests/builds, drives a real browser, audits the UI, debugs failures, and only calls something done when every
requirement is **verified** (or blocked / waived).

It works **out of the box with no account**: the first-run provider is a deterministic offline model that drives the full loop on bundled demo projects.
Add Anthropic, OpenAI, Qwen/DashScope, DeepSeek, Gemini, OpenRouter, Ollama, LM Studio or any OpenAI-compatible endpoint whenever you like — no rebuild, no config editing.

```
genius demo                  # watch it fix a buggy project (offline)
genius demo --scenario debug # a wrong first fix → diagnosis → repair
genius demo --scenario web   # builds a page; a real browser audit fails it; it repairs it
genius                       # open the TUI in the current project
```

## Install

```bash
./build.sh                                   # builds dist/genius_dev-<version>-py3-none-any.whl and smoke-tests it in a clean venv
pipx install dist/genius_dev-*.whl           # or: uv tool install dist/genius_dev-*.whl
playwright install chromium                  # only for browser testing / visual QA (pip install 'genius-dev[browser]')
genius --install-completion                  # zsh/bash/fish completion
./dev.sh                                     # development: venv + editable install (+ dev, browser extras) then launches genius
```
**Standalone macOS binary (Apple silicon, no Python needed for genius itself):** `./scripts/build_standalone.sh` builds `dist/standalone/genius/genius`; `packaging/homebrew/genius-dev.rb` is the Homebrew formula (publish steps inside).
Requires Python ≥ 3.11 (developed on 3.13) and `git` (checkpoints). macOS is the supported platform; Windows is **not yet verified**.

## Add your first API key later

```bash
genius models                               # every provider and its honest status (READY FOR KEY / NEEDS MODEL / LIVE VERIFIED / …)
genius models key anthropic                 # paste the key (hidden) → macOS Keychain. Output: "Configured: YES" — the key is never printed
genius models select-model anthropic <id>   # or: genius models list-models anthropic
genius models test anthropic                # one small real request → LIVE VERIFIED or ERROR with the reason
genius models set-primary anthropic         # use it for everything (or per role: genius model implementer --use qwen)
```
Same for `qwen`, `openai`, `deepseek`, `gemini`, `openrouter`. **Select Qwen:** `genius models key qwen && genius models select-model qwen <model-id> && genius models set-primary qwen`.
**Select Claude:** the same with `anthropic`. Fallbacks: `genius models set-fallback anthropic openai`. In the TUI: `^O`, pick a row, then `a` add key · `t` test · `m` model · `p` primary · `f` fallback · `c` configure · `x` remove key.
Local: `ollama pull <tag>` then `genius models add ollama && genius models select-model ollama <tag> && genius models test ollama`, and `genius --local` keeps everything on-machine.

A provider showing **READY FOR KEY** is fully implemented and mock-tested; only your credential is missing (PROVIDERS.md explains the two-stage verification).

## Everyday use

Type naturally in the TUI (or `genius run "…"`): *Build me a delivery management app.* · *Fix merchant signup and make sure login actually works.* ·
*Find everything preventing this app from being production ready.* · *Continue where you stopped yesterday.*
While it works: `stop`, `pause`, `resume`, `use qwen for coding`, `change model to claude`, `don't touch the frontend`, `restore last checkpoint`, `skip this requirement`, `show me what changed`, `remember that we use tabs`.
Switch to autonomous mode: `permissions autonomous` in the prompt, `genius run "…" -p autonomous`, or Settings → Permissions (`SAFE` asks for everything, `STANDARD` is the default, `AUTONOMOUS` builds/tests/fixes without routine prompts but still asks for destructive, external, production and secret-touching actions).

| Command | What it does |
|---|---|
| `genius` / `tui` | Open the TUI |
| `new` / `open [path]` | Create a project / inspect an existing repo and write `.genius/` memory (no code changes) |
| `plan "goal"` | Requirements, tasks, likely files, risks — modifies nothing |
| `run "goal" [--headless] [--json] [-p safe\|standard\|autonomous] [--local]` | The autonomous loop |
| `resume` | Rebuild last session state (recovering from crashes), re-inspect, continue |
| `finish [--no-fix]` | Deep completion audit; repairs and repeats until PASS or a real blocker |
| `status` `tasks` `requirements [--import f.json] [--verify]` `costs [--by role]` `budget N` `models` `model` `context` `search` | Inspect (all support `--json`) |
| `search "where is password reset handled"` / `--symbol Name` / `--file f` / `--text s` | Offline semantic + symbol + file + text search |
| `test` `build` `preview` `processes` `stop` `restart` `logs` `diff` | Run and observe |
| `checkpoint` `checkpoints` `restore <id>` `undo` | Git-backed, non-intrusive checkpoints |
| `review [--ui]` `debate "q"` `debug` `security [--deps]` | Diff review, UI audit, multi-model debate, static security scan |
| `doctor [--fix] [--quick]` `plugins` `handoff` `clean` `protect <path>` `config` `demo` `help` | Housekeeping |

TUI shortcuts (terminals cannot send ⌘; Ctrl is used, all remappable via `[ui.keys]`): `^K` palette · `^P` tasks · `^L` logs · `^T` tests · `^D` diff · `^V` preview · `^O` models · `^R` resume · `^X` stop · `esc` home.
(`^M` is Enter in every terminal, so *models* is `^O`.) The palette also does global fuzzy + semantic search and every provider action.

## Documentation

ARCHITECTURE · PROVIDERS · MODEL_ROUTING · TOOLS · PLUGINS · PERMISSIONS · MEMORY · SECURITY · TESTING · DEVELOPMENT · **PROJECT_STATUS** (what is verified vs. missing).
