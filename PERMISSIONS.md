# Permissions (`permissions.py`)

| Mode | File edits | Shell |
|---|---|---|
| **SAFE** | ask for every write | ask for every command |
| **STANDARD** (default) | allowed inside the project | LOW and PROJECT risk allowed; HIGH asks |
| **AUTONOMOUS** | as STANDARD, single-file deletes allowed | as STANDARD — HIGH risk still asks |

Always ask (any mode): writes outside the project, protected paths, deleting >50 files, database writes, external HTTP,
publishing/deploying/authenticating (`npm publish`, `login`…), migrations or anything mentioning `prod`, cloud CLIs
(`aws gcloud az kubectl terraform docker vercel …`), `curl/wget/ssh/scp/rsync`, `rm mv chmod kill …`, unrecognised commands, `git push/reset --hard/clean -f/rebase`.
Always **denied**: `sudo`, `rm -rf /|~|.`, fork bombs, `mkfs`, `dd of=/dev/…`, pipe-to-shell installs, force pushes.

Shell classification (`classify_shell`) splits pipelines/`&&`/`;` chains and takes the highest risk of any segment:
LOW (read-only, tests, linters, builds) · PROJECT (installs, git add/commit, mkdir, `rm -rf node_modules`) · HIGH · BLOCKED.

`ask` decisions are resolved by the UI (modal, `y`/`n`; `esc` denies). **Headless runs have no UI, so every `ask` is a denial.**
`genius protect <path>` / "don't touch the frontend" adds protected paths (exact, prefix or glob) persisted in `.genius/config.toml`.

**This is a policy layer, not a sandbox.** A command is judged by its text; a determined model could obfuscate one.
Run untrusted work in a container/VM. See SECURITY.md.
