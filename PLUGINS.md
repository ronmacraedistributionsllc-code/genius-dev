# Plugins

A plugin is a small class that teaches Genius Dev about a toolchain. It can contribute **detection**, **preview behaviour**,
**doctor checks** and **agent tools**. Everything built in (Python, Node/npm, Git, Playwright, Docker, and the Rust/Go/Ruby/PHP/JVM/Xcode/Flutter
toolchains) is an ordinary plugin using the same API — `genius plugins` lists them.

```python
# ~/.genius-dev/plugins/shopify.py
from genius_dev.plugins import Check, Plugin, PluginTool, PreviewPlan

class Shopify(Plugin):
    name = "shopify"
    description = "Shopify theme development"

    def detect(self, root, info):                      # fill ProjectInfo (called on every project inspection)
        if (root / "config" / "settings_schema.json").exists():
            info.add("frameworks", "Shopify")
            info.test_cmd, info.dev_cmd, info.dev_port, info.kind = "shopify theme check", "shopify theme dev", 9292, "web"

    def preview(self, project) -> PreviewPlan | None:  # how `genius preview` should run this project
        if "Shopify" in project.info.frameworks:
            return PreviewPlan("web-server", "Shopify theme", "shopify theme dev --port {port}", 9292, plugin=self.name)

    async def doctor(self, project) -> list[Check]:    # must run something real (a binary, a request, a file open)
        import shutil
        return [Check("Shopify CLI", "ok" if shutil.which("shopify") else "warn", "found" if shutil.which("shopify") else "not installed")]

    def tools(self) -> list[PluginTool]:               # extra tools the agent may call
        async def theme_files(tb):
            from genius_dev.tools import ToolResult
            return ToolResult(True, "theme files", "\n".join(p.name for p in tb.p.root.glob("**/*.liquid")))
        return [PluginTool("theme_files", "List Liquid templates.", {}, theme_files, "THEME")]

PLUGINS = [Shopify()]                                   # or:  def plugin(): return Shopify()
```

## API (version 1)

| Hook | Signature | Used by |
|---|---|---|
| `detect(root, info)` | mutate `ProjectInfo`: `languages frameworks package_managers databases services test_framework test_cmd build_cmd lint_cmd typecheck_cmd dev_cmd dev_port kind` | project open, context, doctor, finish |
| `preview(project) -> PreviewPlan \| None` | `PreviewPlan(kind, title, command, port, steps, available, reason, notes)`; kinds: `web-server desktop-app cli compose ios-simulator android-emulator none`; set `available=False, reason="…"` to say exactly why something can't be previewed | `genius preview` |
| `async doctor(project) -> list[Check]` | `Check(name, status ok\|warn\|fail\|skip, detail, fix="")` | `genius doctor`, Doctor view |
| `tools() -> list[PluginTool]` | `PluginTool(name, description, params, async fn(toolbox, **args) -> ToolResult, label, category)` | the agent (advertised to the model, permission-logged like core tools) |

Set `api_version = 1` (the default). A plugin declaring another version is rejected with a message. Tool names may not collide with core tools.

## Where plugins load from

1. **Built-ins** (this repository, `plugins.py`).
2. **Entry points**: a pip-installable package can expose group `genius_dev.plugins` → a `Plugin` subclass, instance or factory.
3. **`~/.genius-dev/plugins/*.py`** — your own plugins; always loaded.
4. **`<project>/.genius/plugins/*.py`** — loaded **only** if `[plugins] trust_project = true` (Settings → Advanced). Project plugins are arbitrary code from the repository; the default is *not* to run them (an explanatory warning appears in `genius plugins` and `genius doctor`).

A plugin that fails to import, crashes in a hook, has a duplicate name, or targets another API version is skipped and reported — it never breaks detection, doctor or the agent.

## Built-in plugin tools

| Tool | Plugin | Purpose |
|---|---|---|
| `python_env` | python | installed packages of the project interpreter |
| `npm_scripts` | node | scripts from `package.json` |
| `git_summary` | git | branch, changed files, recent commits |
| `browser_available` | playwright | launches Chromium for real and reports the result |
| `docker_ps` | docker | running containers |

## Adding a future platform (Android, Xcode, WordPress, Supabase, Firebase, AWS, audio tooling…)

Write a plugin that (1) detects the platform's marker files, (2) sets `test_cmd/build_cmd/dev_cmd`, (3) returns a `PreviewPlan` (or an honest
`available=False, reason=…`), (4) checks its CLI in `doctor()` by running it, and (5) optionally exposes read-only inspection tools. The
simulator/emulator previews for Xcode and Android already exist in `preview.py` as reference implementations (implemented; see PROJECT_STATUS for what was verified).
