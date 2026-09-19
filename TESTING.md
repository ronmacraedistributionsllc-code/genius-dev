# Testing

```bash
./dev.sh test                 # everything (~4 min; TUI + browser + preview tests dominate)
./dev.sh test tests/test_router.py -q
./dev.sh shots /tmp/shots 120x36 run tasks settings     # render TUI screens to PNG for visual review
```

| File | Covers |
|---|---|
| `test_providers.py`, `test_providers_more.py` | every adapter × {success, streaming, tool call, invalid shape, malformed JSON, bad auth, 429, 5xx, timeout/transport, context overflow} against mocked HTTP; health; cost/cache maths; secret-free errors; provider **status semantics** (READY FOR KEY / NEEDS MODEL / KEY SET · UNTESTED / LIVE VERIFIED / ERROR / NOT CONFIGURED / CONNECTED) and key handling |
| `test_router.py` | modes, custom/pins, explanations, fallback chains, auth never retried, rate-limit retry-once, all-fail, budget states, cheaper/local switch, over-budget hard stop + grace, privacy redaction and caps, vision routing |
| `test_permissions.py` | ~40 shell classifications, mode matrix, protected paths, unlock, headless denial |
| `test_tools.py` | every core tool, path escape, secret withholding, redaction, logging, bad args, gates |
| `test_state.py` | requirements/definition of done, tasks, checkpoints (restore/undo/secrets/user git untouched/non-git), memory persistence, index (AST, routes, tables, incremental), fuzzy search, context selection/secrets/budget, compaction, detectors, test-output parsers, failure classes |
| `test_agent.py` | end-to-end demo, failure→repair, "model claims success" ≠ PASS, stuck detection, stop/pause, plan mode, SAFE mode + approvals, protected paths, no-provider, fallback run, resume, compaction, malformed output, final review gating, controller overrides, crash recovery |
| `test_search.py` | offline semantic search (concept expansion, typos, chunks for components/docs/tests, incremental), context blending, `genius search` modes |
| `test_plugins_preview.py` | plugin loading/isolation/trust, plugin tools/doctor/preview, web preview (real server, health, browser errors, port conflicts), CLI/Compose plans, iOS-Simulator and Android flows with faked platform tools, honest "cannot preview because …" |
| `test_scenarios.py` | the mock model's deterministic scenarios: debug (wrong first fix → diagnosis → repair), web (real browser audit fails → repair → passes), demo command phases |
| `test_self_audit.py` | Genius Dev audits itself: documented commands exist, palette items live, views reachable, intents dispatched, tools documented, no placeholder code, no secrets |
| `test_services.py` | handoff, finish audit (+ loop termination), security scan, review, debate (independence, evidence, read-only judge), doctor, process manager, intents, config layering |
| `test_cli.py` | Typer commands incl. `--json` streams and exit codes, demo |
| `test_tui.py` | Textual pilot: every view at 60×18 / 80×24 / 120×36 / 200×55 (no line wider than the viewport), shortcuts, palette + search, selection, settings, dialogs, logs filter/search/expand, key remap |
| `test_browser.py` | real Chromium: signup→logout→login→create→reload→verify persistence, failure reporting, DOM audit (overflow/contrast/a11y/dead links), console + network capture, `visual_qa` with a managed server |

**Not covered (and why):** live cloud providers — *LIVE AUTHENTICATION VERIFICATION*, needs your keys (see PROVIDERS.md); a real vision model; a running Ollama/LM Studio model; a real Xcode/Android project (the simulator/emulator flows are tested with faked `xcodebuild`/`simctl`/`adb`/`gradle`); non-macOS platforms.
