# Project status

## GENIUS DEV V1

```
SOFTWARE REQUIREMENTS        12 / 12 PASS   (V1_REQUIREMENTS.json — every item re-verified from fresh evidence by `genius finish`, no model involved)
TESTS                        490 / 490 PASS (0 failed)   ·   `genius finish` on this repository: PASS, no findings
LINT (ruff)                  PASS          TYPES (mypy)   PASS
BUILD / PACKAGE              PASS          (wheel built, installed into a clean environment, `genius --help`, `doctor`, `demo`, `models`, `status` verified)
TUI VISUAL QA                PASS          (every screen inspected at 80×24, 120×36 and 200×56)

EXTERNAL VERIFICATION (needs your credentials — does not block V1)
Anthropic                    READY FOR KEY
Qwen / DashScope             READY FOR KEY
OpenAI                       READY FOR KEY
DeepSeek                     READY FOR KEY
Gemini                       READY FOR KEY
OpenRouter                   READY FOR KEY
Ollama                       NOT CONFIGURED   (no model pulled)
LM Studio                    NOT CONFIGURED   (no server)
Mock (offline)               CONNECTED
```

Provider verification has two stages (PROVIDERS.md): **implementation verification** (adapter exists, shapes follow the documented API, 10-case mocked matrix passes) is
PASS for every provider; **live authentication verification** is *NOT TESTED — KEY NOT CONFIGURED*. That is not a failed build; it means the integration is ready for your key.

## COMPLETED

* **CLI** — every command in the brief plus `search`, `plugins`, `protect`, `init`, `tui`; `--json` on data commands; shell completion; provider/checkpoint completion on arguments.
* **Providers** — Anthropic, OpenAI, Qwen/DashScope, DeepSeek, Gemini, OpenRouter, Ollama, LM Studio, any OpenAI-compatible endpoint, plus the offline mock. Honest status model (READY FOR KEY … LIVE VERIFIED), Keychain key management (`models key/remove-key/test/select-model/list-models/configure/set-primary/set-fallback/remove`), keys never printed, first run needs no account.
* **TUI** — 15 views incl. Models (provider setup: add key / test / select model / primary / fallback / configure / remove key, hidden-input dialog) and Doctor, per-file diff drill-down, approval dialogs that show the actual colored change, responsive down to 60×18, palette with fuzzy + semantic search and every provider action.
* **Mock provider drives the whole app** — scripted, deterministic scenarios: `fix` (two bugs), `debug` (shallow first fix fails → diagnosis from evidence → repair), `web` (page built → real browser audit fails → repaired → passes); scripted/failing/slow variants for tests (fallback, budget, stuck loops, compaction, resume, crash). `genius demo [--scenario fix|debug|web]` narrates the 14-step loop.
* **Router** — five modes, roles, explanations, pins, primary provider, fallback chains, bounded retries, auth never retried, budget (near / over / hard stop + 10% grace), privacy caps and redaction (with a visible `REDACT` event), cost tracking by provider/model/role.
* **Agent loop** — requirements → plan → checkpoint → implement → lint/typecheck/tests/build → per-requirement verification (tests, build, commands, **browser audit**) → repair rounds → visual QA → review → memory (incl. auto-generated `architecture.md`, `preferences.md`). Stuck detection with escalation to the stronger model, stop/pause/resume, compaction, crash recovery, resume.
* **Semantic search** — offline BM25 + concept thesaurus + typo tolerance over symbol/section chunks; components, routes, tables, tests, docs, requirements, tasks; integrated into context selection and `genius search`.
* **Plugins** — documented API (detect / preview / doctor / tools), six built-in plugins, entry-point + user + trusted-project loading, error isolation, `genius plugins`.
* **Preview** — dev-server start, port detection, HTTP health, browser error capture, open-in-browser, process management; Electron, CLI, Compose; Xcode/iOS-Simulator and Android-emulator flows; exact reasons when a platform can't be previewed.
* **Doctor** — every check performs the thing it reports (runs binaries, `docker info`, launches Chromium, `PRAGMA integrity_check`, `git fsck`, re-indexes, real mock round-trip, real lint/typecheck/build/tests unless `--quick`).
* **Finish** — TODO/FIXME/stubs (`pass`/`...` bodies via AST)/NotImplemented/placeholders/mock data/dead handlers/secrets/docs/packaging/gates/UI audit; repair loop that terminates. Run against Genius Dev itself (see below).
* **Browser QA** — DOM audit (overflow, clipping, overlap, contrast, tiny text/tap targets, labels, dead links, large empty areas) at desktop/tablet/mobile + optional axe-core (`genius-dev[a11y]`).
* Checkpoints, memory, requirements/DoD, handoff, review, debate, security, budgets, permissions, protected paths — as before, all under test.
* Test-output parsers: pytest, unittest, Jest/Vitest, Cargo, Go, Gradle, Flutter, XCTest/swift, Playwright, Cypress.

## TESTED

See TESTING.md. In addition to the unit/integration suites: `genius finish --no-fix` was run against Genius Dev itself and ends **PASS** (12/12 requirements, 490/490 tests, lint, types, build, packaging, docs, no placeholders/secrets). Along the way it (and the other real runs) found and fixed: a crash indexing empty Markdown files, home-directory/`~/.genius` being mistaken for a project (now refused; global data moved to `~/.genius-dev`), a mypy invocation that ignored `files=`, secret-scan false positives on key *references*, a crash on Django `path('', …)` routes, noisy `SyntaxWarning`s when indexing files with regex escapes, Python-3.12-only f-string syntax in a package declaring 3.11 support, an unquoted interpreter path with spaces (doctor), and a shell-parsing bug in the command classifier (quoted `;`, found by the web demo).

## KNOWN ISSUES / LIMITATIONS

* **Live provider behaviour is unverified** (needs your keys). Mocked shapes follow the documented APIs; a real key may reveal model-specific parameter differences.
* **Real feature-building quality depends on the model you connect.** The mock only knows its scenarios; on other projects it honestly declines to edit.
* Simulator/emulator previews are implemented but were exercised only with faked `xcodebuild`/`simctl`/`adb`/`gradle`, not against a real Xcode/Android project.
* Semantic search is lexical-semantic (BM25 + concept expansion), not neural embeddings: it will not bridge arbitrary paraphrase.
* The agent uses a JSON-in-text protocol, not provider-native tool-calling (adapters expose tool calls via `generate`, the loop does not use them).
* Shell safety is heuristic classification, **not a sandbox**.
* Terminals can't send ⌘; Ctrl shortcuts are used and `^M` is unusable (Enter), so models = `^O`.
* Approval dialogs show the full proposed change but accept or reject it as a whole (no per-hunk partial accept).
* Vision review is implemented but never run against a real vision model; the DOM/axe audit is the tested path.
* `docs_lookup` searches local docs only (no web).
* Dependency vulnerability scanning is limited to `npm audit`/`pip-audit` when present.
* **Windows: NOT YET VERIFIED.** macOS is the supported platform; the architecture keeps platform assumptions (process groups, `/dev/null`, shell) in `runner.py`, `procs.py`, `permissions.py`.
* Dev servers started by a session that crashed keep running until `genius stop` (`genius resume` and `doctor` list them).

## EXTERNAL BLOCKERS (do not block V1)

* Your API keys for live authentication of Anthropic / OpenAI / Qwen / DeepSeek / Gemini / OpenRouter.
* A pulled Ollama model or running LM Studio server for live local-provider verification.
* Homebrew tap / PyPI publication (a wheel is built and install-tested).

## NOT IMPLEMENTED

* Neural embedding retrieval; per-hunk partial approval; web documentation lookup; Windows support; plugins for AWS/Supabase/Firebase/WordPress/Shopify/audio (the API and Xcode/Android/Compose previews exist; those platform plugins are yours to add — see PLUGINS.md).

## NEXT WORK

1. Add a key and run `genius models test <name>`, then a real task; fix any live-API differences.
2. Native tool-calling path behind the same `ToolBox`.
3. Local-model run (Ollama/LM Studio) and prompt tuning for small models.
4. Real Xcode/Android sample projects for the preview flows; PyPI + Homebrew packaging.
