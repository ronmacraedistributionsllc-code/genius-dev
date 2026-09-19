# Development

```bash
./dev.sh                 # venv (uv, Python 3.13) + `pip install -e .[dev,browser]`, then runs `genius`
./dev.sh test            # pytest
./build.sh               # wheel/sdist + clean-venv smoke test (`genius demo`)
playwright install chromium
```

Layout: `src/genius_dev/` (see ARCHITECTURE.md), `tests/`, `scripts/tui_shot.py` (headless TUI → PNG using Textual's SVG export + Chromium).

Conventions: views are pure render functions in `tui/views.py`; never `await` a dialog inside a Textual message handler
(use `run_worker`); every tool returns `ToolResult` and never raises; anything user-visible passes through `redact`;
new behaviour needs a test using `MockProvider` (scripted via `provider.script = [json, …]`, failures via `provider.fail_with`).

The mock model speaks the same JSON protocol as real models. Its "brain" only knows the demo project; elsewhere it declines to edit.

## Quality gates for Genius Dev itself

```bash
./dev.sh test                       # full suite
.venv/bin/ruff check src scripts tests
.venv/bin/mypy                      # strict enough to catch Optional/attr errors; config in pyproject.toml
genius config tools.shell_timeout 1200 --project   # once: the full suite takes ~5 min, longer than the default 300 s gate timeout
genius finish --no-fix              # Genius Dev audits itself (tests, build, lint, typing, TODO/stub/placeholder scan, docs, packaging, secrets)
```
`tests/test_self_audit.py` fails the build on dead commands documented in the README, dead palette items, unreachable views, unhandled intents,
undocumented tools, placeholder code, or secrets in the tree. Lines that legitimately contain marker words (the audit's own patterns) carry `# genius:ignore`.

The V1 acceptance checklist lives in `V1_REQUIREMENTS.json`; `genius requirements --import V1_REQUIREMENTS.json && genius requirements --verify` re-verifies every item from fresh evidence
(no model involved), and `genius finish` does the same before auditing, so a stale PASS can never survive.
