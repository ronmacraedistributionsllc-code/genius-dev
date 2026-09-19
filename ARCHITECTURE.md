# Architecture

Python 3.11+, [Textual](https://textual.textualize.io) for the TUI, Typer for the CLI, `httpx` for providers,
SQLite for state, Playwright for browsers. Everything is async at the edges; SQLite calls are short and synchronous.

```
cli.py ──┐                                   ┌── providers/  (base, openai_compat, anthropic, gemini, mock)
tui/app.py ─► controller.py ─► agent.py ─────┼── router.py   (routing, fallback, budget, cost accounting, privacy)
             intent.py         │             ├── tools.py    (25 tools; permission + logging wrapper)
                               │             ├── context.py  (context selection, compaction)
                               │             └── permissions.py, secrets.py
                               ├── project.py (config, .genius memory, index, requirements, tasks, checkpoints, procs)
                               ├── runner.py (subprocess + output parsers), detect.py (plugin detectors)
                               └── browser.py, visual.py (Playwright flows + DOM audit + optional vision review)
audit.py (finish/security/review) · debate.py · doctor.py · handoff.py · status.py · events.py · db.py · config.py
```

## The loop (`agent.py`)

1. **Understand / inspect** – incremental index refresh (`indexer.py`).
2. **Requirements** – the *planner* role returns JSON `{requirements[{description, verify, files}], tasks[]}`. Each
   requirement gets an id and a verification method (`tests`, `build`, `cmd:<shell>`, `browser`, `manual`).
3. **Checkpoint** – a private git snapshot (`refs/genius/cp/*`), see MEMORY.md.
4. **Implement** – the *implementer* converses with the model in a JSON protocol
   (`{"summary","actions":[{"tool","args"}],"done","final"}`), max 24 steps × 6 actions per round. Tool results are
   truncated (head+tail) before returning to the model. Provider-native tool-calling is exposed by the adapters but the
   loop deliberately uses the text protocol so every provider, including small local models, behaves identically.
5. **Verify** – lint → typecheck → tests → build run, then **each requirement's status is set from evidence only**
   (`PASS` needs a passing check; "no tests found" is *not* a pass). Model confidence is never consulted.
6. **Repair** – on failure the *debugger* role gets the real failure output; up to 3 rounds. An identical failure
   signature across rounds, repeated identical actions, ≥6 edits of one file, or 5 test runs without a change all raise
   `STUCK`, inject a "take a different approach" message, and end the round if it persists.
7. **Visual QA** (web projects with frontend changes) → **final review** (skipped for tiny diffs) → report → memory.

Context: `ContextBuilder` ranks files by path/symbol overlap with the goal, adds their tests, open requirements, recent
decisions, errors and a diff stat under a token budget; secret files are never included and secrets inside files are
redacted. Usage is tracked against the selected provider's context window; at ≥70% the older turns are summarised by
the cheap model, written to `progress.md`, and the conversation continues.

## Concurrency & UI

The agent runs as an asyncio task inside the Textual loop; blocking work (indexing, git) uses `asyncio.to_thread`.
`EventBus` fans events to the activity feed, log view, SQLite (`events`) and the `--json` stream. The TUI renders each
view as a pure function `(app, width, height) → Rich renderable` — no bordered widgets, only spacing and hairlines.

## Extension points

* **Plugins** – detection, preview, doctor checks and agent tools; built-ins and third parties share one API (see PLUGINS.md).
* **Providers** – subclass `ModelProvider`, register in `providers.KINDS`.
* **Tools** – add a method + `_t(...)` registration in `ToolBox._register` (name, description, param schema, label).
* Future platforms (Android/Xcode/AWS/Supabase/…) are added as plugins; Xcode-simulator and Android-emulator previews already exist in `preview.py`.

## Repository intelligence and semantic search (`indexer.py`, `semantic.py`)

The index (SQLite, incremental by mtime+size) holds files, symbols (Python via `ast`; regex for JS/TS/Go/Rust/Ruby/PHP/Java/Kotlin/Swift/Dart), routes,
tables/models, imports and **retrieval chunks** (one per symbol body, markdown section and file head). `genius search` combines fuzzy file/symbol/route/table
matching, exact/regex text search, and **semantic search**: BM25 over chunks with identifier-aware tokenisation (camelCase/snake_case), light stemming, a built-in
software-concept thesaurus (a query about "authentication" also finds login/session/token/password code), typo tolerance, and title/path boosting. Requirements and
tasks are searched the same way. It is **fully offline** — no embedding API. It is lexical-semantic retrieval, not a neural embedding model: it understands software
vocabulary, not arbitrary paraphrase. The same ranking is blended into agent context selection (`RepoIndex.relevant_files`).

## Preview (`preview.py`)

`genius preview` asks plugins for a `PreviewPlan`, else uses the detected dev server. Web servers are started through the process manager (deduplicated, port-checked,
logged), health-checked over HTTP, optionally loaded in Chromium to collect console errors and failed requests, and optionally opened in your browser. Desktop apps (Electron) are launched
and watched; CLI projects run once; Docker Compose starts as a managed process; Xcode projects build and launch in the iOS Simulator (`xcodebuild` + `simctl`); Android projects install
via Gradle and launch on an emulator/device (`adb`). When a platform cannot be previewed the result says exactly why (missing tool, no simulator runtime, no AVD, …) instead of pretending.
