# Tools (`tools.py`)

Every call goes through `ToolBox.execute`, which validates arguments, applies permissions, records a `tool_calls` row
(tool, redacted args, duration, ok, stdout/stderr summary, files) *before and after* running (so crashes are detectable),
emits an event, and returns a structured `ToolResult(ok, summary, output, files, data, denied, failure_class)`.
Output is redacted and truncated (head+tail, default 6000 chars) before it can reach a model.

| Tool | Purpose | Notes |
|---|---|---|
| `read_file` | file or line range | refuses paths outside the project and secret files (`.env`, keys…) |
| `write_file` `patch_file` `move_file` `delete_file` | edits | patch needs an exact, unique match; protected paths & modes enforced; delete always confirms unless AUTONOMOUS single file |
| `list_directory` `search_files` `search_symbols` `project_index` | navigation | index-backed; `project_index routes\|tables\|symbols <path>\|relevant <text>` |
| `terminal` | shell | risk-classified (see PERMISSIONS.md), timeout, process-group kill |
| `git` | status/diff/log/add/commit/branch… | `push pull fetch clone remote reset clean rebase checkout restore` are unavailable to the agent |
| `tests` `build` `lint` `typecheck` | quality gates | detected commands; `tests` parses pytest/unittest/jest/vitest/cargo/go and records `test_runs` |
| `package_manager` | install/add/remove | npm, pnpm, yarn, pip, uv, cargo |
| `process_manager` | start/stop/restart/list/logs dev servers | de-duplicates, checks ports, logs in `.genius/logs`, cleaned up on exit |
| `browser` `screenshot` | Playwright flows + DOM audit | steps: goto click fill press select check wait_for wait reload expect_text expect_url screenshot; captures console errors and failed requests |
| `database` | SQLite queries | read-only unless approved |
| `http` | requests | localhost freely, external hosts need approval |
| `docs_lookup` | search project docs | local markdown only (no web lookup) |
| `logs` `env_inspect` | logs / versions | env var *names* only, never secret values |

Plugins can add tools (`python_env`, `npm_scripts`, `git_summary`, `browser_available`, `docker_ps` ship built in) — see PLUGINS.md.

Test runners detected: pytest, unittest, Vitest, Jest (via package scripts), Cargo, Go, Gradle, Flutter, XCTest / `swift test`, Playwright and Cypress — all with output parsers. Adding a runner = a detector in `detect.py` + an output parser in `runner.parse_tests`.
