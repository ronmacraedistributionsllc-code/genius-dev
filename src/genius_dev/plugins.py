"""Plugin system. A plugin can contribute: project detection (languages, frameworks, test/build/lint/dev commands),
preview behaviour, real doctor checks, and agent tools. Built-ins below are ordinary plugins using the same API.

Where plugins come from (in order):
  1. built-ins in this file
  2. installed packages exposing an entry point in group ``genius_dev.plugins`` (a Plugin subclass, instance or factory)
  3. ``~/.genius-dev/plugins/*.py``           — the user's own plugins (always loaded)
  4. ``<project>/.genius/plugins/*.py``   — loaded only if ``[plugins] trust_project = true`` (project code runs with your rights)
A plugin module defines ``PLUGINS = [MyPlugin(), ...]`` or ``def plugin() -> Plugin``. Errors in a plugin are recorded and never break Genius Dev.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

API_VERSION = 1


def python_executable() -> str:
    from .detect import python_executable as _pe
    return _pe()


@dataclass
class Check:
    name: str
    status: str            # ok | warn | fail | skip
    detail: str = ""
    fix: str = ""


@dataclass
class PluginTool:
    name: str
    description: str
    params: dict[str, str]                                    # same schema strings as core tools: "str, description" ("?" suffix = optional)
    fn: Callable[..., Awaitable[Any]]                         # async (toolbox, **args) -> ToolResult
    label: str = "PLUGIN"
    category: str = "TOOLS"


@dataclass
class PreviewPlan:
    kind: str                                                 # web-server | desktop-app | cli | ios-simulator | android-emulator | compose | none
    title: str = ""
    command: str = ""
    port: int = 0
    steps: list[str] = field(default_factory=list)            # human-readable steps (used for simulators)
    available: bool = True
    reason: str = ""                                          # when unavailable: exactly why
    notes: list[str] = field(default_factory=list)
    plugin: str = ""


class Plugin:
    """Base class. Override only what you contribute."""
    name = "plugin"
    description = ""
    api_version = API_VERSION

    def detect(self, root: Path, info) -> None:               # fill ProjectInfo: languages, frameworks, test_cmd, build_cmd, dev_cmd …
        return None

    def preview(self, project) -> PreviewPlan | None:
        return None

    async def doctor(self, project) -> list[Check]:           # must perform real checks (run a binary, open a file), not inspect config
        return []

    def tools(self) -> list[PluginTool]:
        return []

    def contributes(self) -> list[str]:
        out = []
        for hook in ("detect", "preview", "doctor", "tools"):
            if getattr(type(self), hook) is not getattr(Plugin, hook):
                out.append(hook)
        return out


class FunctionPlugin(Plugin):
    """Wraps a plain detector function (legacy @detector and the built-in detectors)."""
    def __init__(self, name: str, fn, description: str = ""):
        self.name, self._fn, self.description = name, fn, description

    def detect(self, root, info):
        self._fn(root, info)

    def contributes(self):
        return ["detect"]


async def _run(cmd: str, cwd: Path | str, timeout: float = 15):
    from .runner import run_command
    return await run_command(cmd, Path(cwd), timeout)


async def _version_check(name: str, binary: str, cmd: str, required: bool = False, hint: str = "") -> Check:
    if not shutil.which(binary):
        return Check(name, "warn" if required else "skip", "not installed" + (f" — {hint}" if hint else ""))
    r = await _run(cmd, os.getcwd())
    return Check(name, "ok", (r.output.splitlines() or [""])[0][:70]) if r.ok else Check(name, "fail", f"`{cmd}` failed: {r.output[:80]}")


# ------------------------------------------------------------------------------------------------ built-ins
class PythonPlugin(Plugin):
    name, description = "python", "Python projects: pytest/unittest, ruff, mypy, FastAPI/Flask/Django dev servers"

    def detect(self, root, info):
        from .detect import BUILTIN_DETECTORS
        BUILTIN_DETECTORS["python"](root, info)

    async def doctor(self, project):
        out = [Check("Python", "ok", f"{sys.version.split()[0]} ({python_executable()})")]
        r = await _run(f"{shlex.quote(python_executable())} -m pip --version", os.getcwd())
        no_pip = "skip" if shutil.which("uv") else "warn"          # uv-managed venvs deliberately ship without pip
        out.append(Check("pip", "ok" if r.ok else no_pip, (r.output.splitlines() or [""])[0][:60] if r.ok else "not in this interpreter" + (" (uv manages packages)" if no_pip == "skip" else "")))
        if project and "python" in project.info.languages and project.info.python:
            r = await _run(f"{shlex.quote(project.info.python)} --version", project.root)
            out.append(Check("Project interpreter", "ok" if r.ok else "fail", f"{project.pretty(project.info.python)} · {r.output.strip()}" if r.ok else "cannot run"))
        return out

    def tools(self):
        async def python_env(tb) -> Any:
            from .tools import ToolResult
            r = await tb._sh(f"{shlex.quote(tb.p.info.python or python_executable())} -m pip list --format=freeze")
            return ToolResult(r.ok, f"{len(r.stdout.splitlines())} packages", r.stdout[:3000])
        return [PluginTool("python_env", "List installed Python packages for the project interpreter.", {}, python_env, "ENV")]


class NodePlugin(Plugin):
    name, description = "node", "Node projects: npm/pnpm/yarn scripts, Vite/Next/React/Vue/Svelte, Vitest/Jest/Playwright"

    def detect(self, root, info):
        from .detect import BUILTIN_DETECTORS
        BUILTIN_DETECTORS["node"](root, info)

    async def doctor(self, project):
        out = [await _version_check("Node", "node", "node --version"), await _version_check("npm", "npm", "npm --version")]
        if project and (project.root / "package.json").exists():
            ok = (project.root / "node_modules").is_dir()
            out.append(Check("node_modules", "ok" if ok else "warn", "installed" if ok else "missing — run the package manager install"))
        return out

    def tools(self):
        async def npm_scripts(tb) -> Any:
            import json
            from .tools import ToolResult
            f = tb.p.root / "package.json"
            if not f.exists():
                return ToolResult(False, "no package.json")
            sc = json.loads(f.read_text()).get("scripts", {})
            return ToolResult(True, f"{len(sc)} scripts", "\n".join(f"{k}: {v}" for k, v in sc.items()))
        return [PluginTool("npm_scripts", "List package.json scripts.", {}, npm_scripts, "ENV")]

    def preview(self, project):
        i = project.info
        if "Electron" in i.frameworks and i.dev_cmd:
            return PreviewPlan("desktop-app", "Electron app", i.dev_cmd, 0, plugin=self.name, notes=["launches a native window; the process is managed and logged"])
        return None


class GitPlugin(Plugin):
    name, description = "git", "Git repository health and summary"

    def detect(self, root, info):
        if (root / ".git").exists():
            info.add("services", "git")

    async def doctor(self, project):
        out = [await _version_check("Git", "git", "git --version", required=True)]
        if not shutil.which("git"):
            return out
        r = await _run("git config user.name && git config user.email", os.getcwd())
        out.append(Check("Git identity", "ok" if r.ok else "warn", r.output.replace("\n", " · ")[:60] if r.ok else "user.name/email not set (Genius Dev checkpoints don't need it; your commits do)"))
        if project:
            if not project.git.is_repo:
                out.append(Check("Repository", "warn", "not a git repository — checkpoints will initialise one", "git_init"))
            else:
                r = await _run("git status --porcelain=v1 -b", project.root)
                out.append(Check("Repository", "ok" if r.ok else "fail", f"{project.git.branch()} · {len(project.git.status())} changed file(s)" if r.ok else r.output[:80]))
                fsck = await _run("git fsck --no-progress --connectivity-only", project.root, 60)
                out.append(Check("Repository integrity", "ok" if fsck.ok else "fail", "fsck clean" if fsck.ok else fsck.output.splitlines()[0][:80]))
        return out

    def tools(self):
        async def git_summary(tb) -> Any:
            from .tools import ToolResult
            g = tb.p.git
            if not g.is_repo:
                return ToolResult(False, "not a git repository")
            return ToolResult(True, f"{g.branch()}", "\n".join(["branch: " + g.branch(), *("changed: " + s + " " + f for s, f in g.status()[:40]), *g.log(5)]))
        return [PluginTool("git_summary", "Branch, changed files and recent commits.", {}, git_summary, "GIT")]


class PlaywrightPlugin(Plugin):
    name, description = "playwright", "Browser automation: availability, Chromium launch, test config detection"

    def detect(self, root, info):
        if (root / "playwright.config.ts").exists() or (root / "playwright.config.js").exists():
            info.test_framework = info.test_framework or "playwright"
            info.add("services", "Playwright")

    async def doctor(self, project):
        from .browser import browser_ready, playwright_installed
        if not playwright_installed():
            return [Check("Playwright", "warn", "package not installed — pip install 'genius-dev[browser]' && playwright install chromium")]
        ok, detail = await browser_ready()                       # launches Chromium for real
        return [Check("Playwright", "ok", "package installed"), Check("Browser (Chromium)", "ok" if ok else "warn", "launched and closed OK" if ok else detail)]

    def tools(self):
        async def browser_available(tb) -> Any:
            from .browser import browser_ready
            from .tools import ToolResult
            ok, d = await browser_ready()
            return ToolResult(ok, d)
        return [PluginTool("browser_available", "Check that a real browser can be launched.", {}, browser_available, "BROWSER", "BROWSER")]


class DockerPlugin(Plugin):
    name, description = "docker", "Docker / Compose: daemon reachability, compose preview"

    def detect(self, root, info):
        if any((root / n).exists() for n in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")):
            info.add("services", "Docker")

    async def doctor(self, project):
        if not shutil.which("docker"):
            return [Check("Docker", "skip", "not installed")]
        r = await _run("docker info --format '{{.ServerVersion}}'", os.getcwd(), 15)           # real daemon round-trip
        return [Check("Docker", "ok", f"daemon {r.output.strip()}") if r.ok else Check("Docker", "warn", "installed but the daemon is not reachable — start Docker Desktop")]

    def tools(self):
        async def docker_ps(tb) -> Any:
            from .tools import ToolResult
            if not shutil.which("docker"):
                return ToolResult(False, "docker is not installed")
            r = await tb._sh("docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'", 15)
            return ToolResult(r.ok, "containers" if r.ok else "docker unavailable", r.output)
        return [PluginTool("docker_ps", "List running containers.", {}, docker_ps, "DOCKER")]

    def preview(self, project):
        root = project.root
        f = next((n for n in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml") if (root / n).exists()), None)
        if not f or project.info.dev_cmd:
            return None
        if not shutil.which("docker"):
            return PreviewPlan("compose", "Docker Compose", available=False, reason="docker is not installed", plugin=self.name)
        return PreviewPlan("compose", "Docker Compose", "docker compose up", 0, plugin=self.name, notes=["requires the Docker daemon; ports are defined in the compose file"])


class ToolchainPlugin(Plugin):
    name, description = "toolchains", "Rust, Go, Ruby, PHP, JVM/Android, Xcode, Flutter detection + simulator/emulator previews"

    def detect(self, root, info):
        from .detect import BUILTIN_DETECTORS
        for k in ("rust", "go", "ruby-php", "mobile", "services", "structure"):
            BUILTIN_DETECTORS[k](root, info)

    async def doctor(self, project):
        out = [await _version_check("Rust", "rustc", "rustc --version"), await _version_check("Cargo", "cargo", "cargo --version"), await _version_check("Go", "go", "go version")]
        xc = await _run("xcode-select -p", os.getcwd(), 5) if shutil.which("xcode-select") else None
        out.append(Check("Xcode", "ok", xc.output.strip()) if xc and xc.ok else Check("Xcode", "skip", "not installed"))
        sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        out.append(Check("Android SDK", "ok", sdk) if sdk and Path(sdk).is_dir() else Check("Android SDK", "skip", "ANDROID_HOME not set"))
        return out

    def preview(self, project):
        from .preview import toolchain_preview
        return toolchain_preview(project)


BUILTIN: list[Plugin] = [PythonPlugin(), NodePlugin(), ToolchainPlugin(), GitPlugin(), PlaywrightPlugin(), DockerPlugin()]


# ------------------------------------------------------------------------------------------------ registry
class Registry:
    def __init__(self):
        self.plugins: list[Plugin] = []
        self.errors: list[str] = []
        self.sources: dict[str, str] = {}

    def add(self, pl: Plugin, source: str) -> None:
        if getattr(pl, "api_version", 0) != API_VERSION:
            self.errors.append(f"{getattr(pl, 'name', pl)}: unsupported plugin api_version {getattr(pl, 'api_version', '?')} (need {API_VERSION})")
            return
        if any(p.name == pl.name for p in self.plugins):
            self.errors.append(f"{pl.name}: duplicate plugin name ignored ({source})")
            return
        self.plugins.append(pl)
        self.sources[pl.name] = source

    def _from_obj(self, obj: Any, source: str) -> None:
        if inspect.isclass(obj) and issubclass(obj, Plugin):
            obj = obj()
        elif callable(obj) and not isinstance(obj, Plugin):
            obj = obj()
        if isinstance(obj, Plugin):
            self.add(obj, source)
        else:
            self.errors.append(f"{source}: not a Plugin")

    def load_file(self, f: Path, source: str) -> None:
        try:
            spec = importlib.util.spec_from_file_location(f"genius_plugin_{f.stem}_{abs(hash(str(f)))}", f)
            mod = importlib.util.module_from_spec(spec)                       # type: ignore[arg-type]
            spec.loader.exec_module(mod)                                      # type: ignore[union-attr]
            objs = list(getattr(mod, "PLUGINS", [])) or ([mod.plugin] if hasattr(mod, "plugin") else [])
            if not objs:
                self.errors.append(f"{f.name}: defines neither PLUGINS nor plugin()")
            for o in objs:
                self._from_obj(o, source)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{f.name}: {type(e).__name__}: {e}")

    def tools(self) -> list[tuple[Plugin, PluginTool]]:
        out = []
        for p in self.plugins:
            try:
                out += [(p, t) for t in p.tools()]
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{p.name}.tools failed: {e}")
        return out


def load_registry(root: Path | None = None) -> Registry:
    from .config import Config, global_dir
    reg = Registry()
    for p in BUILTIN:
        reg.add(p, "built-in")
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="genius_dev.plugins"):
            try:
                reg._from_obj(ep.load(), f"entry-point {ep.name}")
            except Exception as e:  # noqa: BLE001
                reg.errors.append(f"entry-point {ep.name}: {e}")
    except Exception:  # noqa: BLE001
        pass
    gdir = global_dir() / "plugins"
    for f in sorted(gdir.glob("*.py")) if gdir.is_dir() else []:
        reg.load_file(f, f"user:{f.name}")
    if root is not None:
        pdir = root / ".genius" / "plugins"
        if pdir.is_dir() and any(pdir.glob("*.py")):
            if Config(root).get("plugins.trust_project", False):
                for f in sorted(pdir.glob("*.py")):
                    reg.load_file(f, f"project:{f.name}")
            else:
                reg.errors.append(f"{pdir}: project plugins found but not loaded — set [plugins] trust_project = true to run them")
    return reg
