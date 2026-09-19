# Project memory (`.genius/`)

```
.genius/
  project.json  requirements.md  architecture.md  decisions.md  progress.md  bugs.md
  current_task.md  providers.json  test_status.json  config.toml
  genius.db          SQLite: projects sessions tasks requirements model_calls tool_calls checkpoints
                     test_runs bugs decisions handoffs events processes   (versioned via PRAGMA user_version)
  index/index.db     files, symbols, routes, tables, imports (incremental by mtime+size)
  checkpoints/  handoffs/  logs/  screenshots/
```

The database is the source of truth; `requirements.md`, `bugs.md`, `providers.json` (no key refs) and `test_status.json`
are regenerated from it (`Project.sync_memory`). `decisions.md`/`progress.md` are append-only narrative.
`~/.genius-dev/global.db` holds model calls across projects (daily budget). `.genius/` is added to `.git/info/exclude` (not your `.gitignore`).

**Requirements** – ids `REQ-001…`; states NOT_STARTED, IN_PROGRESS, PASS, FAIL, BLOCKED, WAIVED. Complete ⇔ every requirement is
PASS, BLOCKED or WAIVED (`Summary.complete`). The "% resolved" figure counts BLOCKED and WAIVED as resolved (per the brief's definition),
so read it next to the PASS count.

**Checkpoints** – created before each run and after it. A checkpoint is a commit object made from a temporary index
(tracked + untracked files, minus ignored, secret files and junk dirs like `node_modules`), stored under `refs/genius/cp/<id>`.
Your branch, HEAD, index and history are never touched. Restore rewrites only files that differ, deletes files that didn't exist,
never touches secret files, and first takes a `pre-restore` checkpoint so a restore is itself undoable. Requirement statuses are restored too.
Non-git folders get `git init` (setting `git.auto_init`).

**Crash recovery** – tool calls are logged as `running` before execution. On the next start `recover_crashed_state` marks dangling
sessions/tool calls `interrupted`, reconciles the process table, and `genius resume` re-runs verification instead of trusting stored status.
Dev servers started by an interrupted session keep running until `genius stop` (their PIDs are tracked).

**Namespaces.** Genius Dev's global data lives in `~/.genius-dev/` (override with `GENIUS_HOME`) — *not* `~/.genius`, which other tools use. Per-project memory is `<project>/.genius/`;
a `.genius` directory is only recognised as project memory if it contains `project.json` or `genius.db`. Your home directory and `/` are never treated as a project
(`genius` refuses with an explanation) so an audit can never index or compile your whole home folder.
