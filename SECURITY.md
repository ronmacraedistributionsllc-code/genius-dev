# Security

* **Credentials** live in the macOS Keychain (`genius models key <name>`, hidden prompt, `keyring`) or environment variables. `genius models`, the Models screen, logs and JSON output only ever say `Configured: YES/NO` — never the key, never a masked fragment. Config files store only references (`env:NAME`, `keychain:acct`); a literal key is ignored.
* **Redaction** (`secrets.py`): Anthropic/OpenAI/Google/GitHub/AWS/Slack keys, JWTs, bearer tokens, private-key blocks and `*_key/secret/token/password = …` assignments. Applied to tool output, events, logs, error messages, handoffs, and every message sent to a non-local provider.
* **Secret files** (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `credentials*`, `.npmrc`, …) are never read by tools, never in context, never in checkpoints, never in handoffs.
* **Privacy controls** (`[privacy]`): `cloud_allowed`, `excluded` dirs, `max_external_context_chars`, `redact_secrets`; `--local` / `local_only` keeps everything on-machine.
* **Git**: the agent cannot push/fetch/reset/clean; checkpoints don't touch your history; `genius finish`/`security` flag tracked secret files and `.env` missing from `.gitignore`.
* **Shell**: heuristic classification + permission modes (PERMISSIONS.md). **Not a sandbox.**
* **Plugins** run Python: `~/.genius-dev/plugins` is yours; project-local plugins are ignored unless `[plugins] trust_project = true`.
* **Known gaps**: dependency-vulnerability scanning is limited to what `npm audit` / `pip-audit` report when installed; the static security scan is regex-based (false positives/negatives); prompt-injection from file contents is mitigated only by the permission layer and tool restrictions (e.g. the debate judge is limited to read-only tools); Keychain access goes through `keyring`'s backend.
