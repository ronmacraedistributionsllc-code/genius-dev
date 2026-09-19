"""Modular agent tools. Every call returns a structured ToolResult and is logged (DB + event bus)."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import shlex
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .browser import playwright_installed, run_flow
from .events import EventBus
from .permissions import Permissions
from .project import Project
from .runner import CmdResult, classify_failure, parse_tests, run_command, truncate
from .secrets import is_secret_file, redact


@dataclass
class ToolResult:
    ok: bool
    summary: str
    output: str = ""
    files: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    denied: bool = False
    duration: float = 0.0
    failure_class: str = ""

    def for_model(self, limit: int = 6000) -> str:
        head = f"{'ok' if self.ok else 'FAILED'}: {self.summary}"
        if self.denied:
            head = f"DENIED: {self.summary}"
        body = truncate(self.output, limit) if self.output else ""
        return head + ("\n" + body if body else "")


@dataclass
class ToolSpec:
    name: str
    description: str
    params: dict[str, str]            # name -> "type, description" ("?" suffix = optional)
    fn: Callable[..., Awaitable[ToolResult]]
    label: str = "TOOL"
    category: str = "TOOLS"

    def schema(self) -> dict[str, Any]:
        props, req = {}, []
        for k, v in self.params.items():
            opt = k.endswith("?")
            k = k.rstrip("?")
            t, _, d = v.partition(",")
            props[k] = {"type": {"str": "string", "int": "integer", "bool": "boolean", "list": "array", "obj": "object"}.get(t.strip(), "string"),
                        "description": d.strip()}
            if not opt:
                req.append(k)
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "properties": props, "required": req}}


class ToolBox:
    def __init__(self, project: Project, perms: Permissions, bus: EventBus, session_id: int | None = None):
        self.p, self.perms, self.bus, self.session_id = project, perms, bus, session_id
        self.changed: set[str] = set()
        self.specs: dict[str, ToolSpec] = {}
        self.max_out = int(project.cfg.get("tools.max_output_chars", 6000))
        self.timeout = float(project.cfg.get("tools.shell_timeout", 300))
        self._register()
        self._register_plugin_tools()

    def _register_plugin_tools(self) -> None:
        import functools
        from .plugins import load_registry
        reg = load_registry(self.p.root)
        for plugin, t in reg.tools():
            if t.name in self.specs:
                reg.errors.append(f"{plugin.name}: tool '{t.name}' collides with an existing tool; skipped")
                continue
            self.specs[t.name] = ToolSpec(t.name, f"[{plugin.name}] {t.description}", t.params, functools.partial(t.fn, self), t.label, t.category)
        self.plugin_errors = reg.errors

    # ---- registry ------------------------------------------------------------------
    def _t(self, name: str, desc: str, params: dict[str, str], fn, label: str, category: str = "TOOLS") -> None:
        self.specs[name] = ToolSpec(name, desc, params, fn, label, category)

    def _register(self) -> None:
        R = self._t
        R("read_file", "Read a text file (optionally a line range).", {"path": "str, relative path", "start?": "int, first line (1-based)", "end?": "int, last line"}, self.read_file, "READ")
        R("write_file", "Create or overwrite a file with full contents.", {"path": "str, relative path", "content": "str, entire file content"}, self.write_file, "WRITE")
        R("patch_file", "Replace an exact, unique snippet in a file.", {"path": "str, relative path", "search": "str, exact text to find", "replace": "str, replacement", "replace_all?": "bool, replace every occurrence"}, self.patch_file, "EDIT")
        R("move_file", "Move/rename a file.", {"src": "str, source path", "dst": "str, destination path"}, self.move_file, "MOVE")
        R("delete_file", "Delete a file or directory inside the project.", {"path": "str, relative path"}, self.delete_file, "DELETE")
        R("list_directory", "List a directory.", {"path?": "str, directory (default .)", "depth?": "int, recursion depth (default 1)"}, self.list_directory, "LIST")
        R("search_files", "Search file contents (text or regex).", {"pattern": "str, text or regex", "regex?": "bool", "glob?": "str, e.g. *.py"}, self.search_files, "FIND")
        R("search_symbols", "Find functions/classes/routes/tables by name (fuzzy).", {"query": "str, name fragment"}, self.search_symbols, "FIND")
        R("terminal", "Run a shell command in the project directory.", {"command": "str, shell command", "timeout?": "int, seconds"}, self.terminal, "RUN")
        R("git", "Run a git subcommand (status, diff, log, add, commit, branch).", {"args": "str, e.g. 'status --short'"}, self.git, "GIT")
        R("tests", "Run the project's test suite (optionally narrowed with extra args, e.g. a path or -k expr).", {"args?": "str, extra runner arguments"}, self.tests, "TEST", "TESTS")
        R("build", "Run the project's build command.", {}, self.build, "BUILD", "BUILD")
        R("lint", "Run the linter.", {}, self.lint, "LINT", "BUILD")
        R("typecheck", "Run the type checker.", {}, self.typecheck, "TYPES", "BUILD")
        R("package_manager", "Install/add packages using the project's package manager.", {"action": "str, install|add|remove", "packages?": "str, space-separated package names"}, self.package_manager, "PKG")
        R("process_manager", "Manage dev servers: start|stop|restart|list|logs.", {"action": "str", "name?": "str, process name", "command?": "str, for start", "port?": "int"}, self.process_manager, "PROC")
        R("browser", "Drive a browser flow against a URL and audit the page (needs Playwright).", {"url": "str", "steps?": "list, [{action,selector,value}]", "audit?": "bool", "viewport?": "str, desktop|mobile"}, self.browser, "BROWSER", "BROWSER")
        R("screenshot", "Take a full-page screenshot of a URL.", {"url": "str", "viewport?": "str, desktop|mobile"}, self.screenshot, "SHOT", "BROWSER")
        R("database", "Run a SQL query against a project SQLite file (read-only unless approved).", {"path": "str, sqlite file", "sql": "str"}, self.database, "DB")
        R("http", "HTTP request (localhost freely; external hosts need approval).", {"url": "str", "method?": "str", "body?": "str"}, self.http, "HTTP")
        R("docs_lookup", "Search project documentation (README, docs/, *.md).", {"query": "str"}, self.docs_lookup, "DOCS")
        R("project_index", "Query the repo index: stats | routes | tables | symbols <path> | relevant <text>.", {"query": "str"}, self.project_index, "INDEX")
        R("logs", "Read recent process logs or agent events.", {"name?": "str, process name (omit for events)", "lines?": "int"}, self.logs, "LOGS")
        R("env_inspect", "Report tool versions and env var NAMES (never values).", {}, self.env_inspect, "ENV")

    def descriptions(self) -> str:
        out = []
        for s in self.specs.values():
            ps = ", ".join(f"{k}: {v.split(',')[0]}" for k, v in s.params.items())
            out.append(f"- {s.name}({ps}) — {s.description}")
        return "\n".join(out)

    # ---- execution wrapper -----------------------------------------------------------
    async def execute(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        args = args or {}
        spec = self.specs.get(name)
        if not spec:
            return ToolResult(False, f"unknown tool '{name}'. Available: {', '.join(self.specs)}")
        sig = inspect.signature(spec.fn)
        bad = [k for k in args if k not in sig.parameters]
        missing = [k for k, p in sig.parameters.items() if p.default is inspect._empty and k not in args]
        if bad or missing:
            return ToolResult(False, f"bad arguments for {name}: " + (f"unexpected {bad} " if bad else "") + (f"missing {missing}" if missing else ""))
        assert self.p.store
        t0 = time.perf_counter()
        row = self.p.store.execute("INSERT INTO tool_calls(ts,session_id,tool,args,status) VALUES(?,?,?,?,'running')",
                                   (time.time(), self.session_id, name, redact(json.dumps(args, default=str))[:2000])).lastrowid
        try:
            res = await spec.fn(**args)
        except asyncio.CancelledError:
            self.p.store.execute("UPDATE tool_calls SET status='cancelled' WHERE id=?", (row,))
            raise
        except Exception as e:  # noqa: BLE001 — tools must never crash the agent
            res = ToolResult(False, f"{type(e).__name__}: {str(e)[:300]}", failure_class=classify_failure(str(e)))
        res.duration = time.perf_counter() - t0
        res.output = redact(res.output)
        self.p.store.execute("UPDATE tool_calls SET duration=?, ok=?, stdout=?, stderr=?, files=?, status='done' WHERE id=?",
                             (res.duration, int(res.ok), res.output[:1500] if res.ok else "", "" if res.ok else res.output[:1500],
                              json.dumps(res.files), row))
        self.changed.update(f for f in res.files if spec.label in ("WRITE", "EDIT", "MOVE", "DELETE"))
        self.bus.emit(spec.category if res.ok else "ERRORS", spec.label if res.ok else "FAIL",
                      res.summary if res.ok else f"{name}: {res.summary}", detail=res.output, tool=name, duration=round(res.duration, 3))
        return res

    # ---- path helpers ----------------------------------------------------------------
    def _rel(self, path: str) -> tuple[Path, str]:
        p = (self.p.root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        try:
            rel = str(p.relative_to(self.p.root))
        except ValueError:
            rel = str(p)
        return p, rel

    @staticmethod
    def _preview(old: str, new: str, rel: str, limit: int = 14) -> str:
        import difflib
        lines = [l.rstrip("\n") for l in difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=1)][2:]
        return "\n".join(lines[:limit]) + (f"\n… +{len(lines) - limit} more lines" if len(lines) > limit else "")

    async def _guard_write(self, rel: str, p: Path, kind: str, count: int = 0, preview: str = "") -> ToolResult | None:
        d = self.perms.check_write(rel if self.perms.inside_project(p) else str(p), kind, count)
        if preview and d.action == "ask":
            d.reason = f"{d.reason}\n\n{preview}"           # let the user review the actual change before approving
        if not await self.perms.resolve(d, f"{kind} {rel}"):
            return ToolResult(False, f"{d.reason} — not permitted", denied=True)
        return None

    # ---- file tools ------------------------------------------------------------------
    async def read_file(self, path: str, start: int = 1, end: int = 0) -> ToolResult:
        p, rel = self._rel(path)
        if not self.perms.inside_project(p):
            return ToolResult(False, f"{path} is outside the project", denied=True)
        if is_secret_file(p.name):
            return ToolResult(False, f"{rel} looks like a secrets file — contents withheld", denied=True)
        if not p.is_file():
            return ToolResult(False, f"{rel} not found")
        text = p.read_text(errors="replace")
        lines = text.splitlines()
        sel = lines[max(0, start - 1): end or None]
        body = "\n".join(f"{i}: {l}" for i, l in enumerate(sel, max(1, start)))
        return ToolResult(True, f"{rel} ({len(lines)} lines)", body, [rel])

    async def write_file(self, path: str, content: str) -> ToolResult:
        p, rel = self._rel(path)
        existed = p.exists()
        old = p.read_text(errors="replace") if existed else ""
        if (deny := await self._guard_write(rel, p, "write", preview=self._preview(old, content, rel))):
            return deny
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return ToolResult(True, f"{'updated' if existed else 'created'} {rel} (+{content.count(chr(10)) + 1} lines)", files=[rel],
                          data={"added": content.count("\n") + 1, "removed": old.count("\n") + 1 if existed else 0})

    async def patch_file(self, path: str, search: str, replace: str, replace_all: bool = False) -> ToolResult:
        p, rel = self._rel(path)
        if not p.is_file():
            return ToolResult(False, f"{rel} not found")
        text = p.read_text(errors="replace")
        n = text.count(search)
        if n == 0:
            return ToolResult(False, f"search text not found in {rel}; re-read the file and copy the snippet exactly")
        if n > 1 and not replace_all:
            return ToolResult(False, f"search text matches {n} places in {rel}; include more context or set replace_all")
        new_text = text.replace(search, replace) if replace_all else text.replace(search, replace, 1)
        if (deny := await self._guard_write(rel, p, "edit", preview=self._preview(text, new_text, rel))):
            return deny
        p.write_text(new_text)
        return ToolResult(True, f"patched {rel} (+{replace.count(chr(10)) + 1}/-{search.count(chr(10)) + 1})", files=[rel],
                          data={"added": replace.count("\n") + 1, "removed": search.count("\n") + 1})

    async def move_file(self, src: str, dst: str) -> ToolResult:
        a, ra = self._rel(src)
        b, rb = self._rel(dst)
        for p, r in ((a, ra), (b, rb)):
            if (deny := await self._guard_write(r, p, "move")):
                return deny
        if not a.exists():
            return ToolResult(False, f"{ra} not found")
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(a), str(b))
        return ToolResult(True, f"moved {ra} → {rb}", files=[ra, rb])

    async def delete_file(self, path: str) -> ToolResult:
        p, rel = self._rel(path)
        count = sum(1 for _ in p.rglob("*") if _.is_file()) if p.is_dir() else 1
        if not self.perms.inside_project(p) or p == self.p.root:
            return ToolResult(False, "refusing to delete outside the project or the project root", denied=True)
        # deletion is never silent in SAFE/STANDARD; AUTONOMOUS may delete single files
        d = self.perms.check_write(rel, "delete", count)
        if self.perms.mode != "autonomous" and d.action == "allow":
            d.action = "ask"
            d.reason = "deletions require confirmation"
        if not await self.perms.resolve(d, f"delete {rel}"):
            return ToolResult(False, f"{d.reason} — not permitted", denied=True)
        if not p.exists():
            return ToolResult(False, f"{rel} not found")
        shutil.rmtree(p) if p.is_dir() else p.unlink()
        return ToolResult(True, f"deleted {rel}" + (f" ({count} files)" if count > 1 else ""), files=[rel])

    async def list_directory(self, path: str = ".", depth: int = 1) -> ToolResult:
        p, rel = self._rel(path)
        if not p.is_dir():
            return ToolResult(False, f"{rel} is not a directory")
        skip = set(self.p.cfg.get("privacy.excluded"))
        out: list[str] = []
        def walk(d: Path, lvl: int) -> None:
            for c in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name)):
                if c.name in skip or len(out) > 300:
                    continue
                out.append("  " * lvl + c.name + ("/" if c.is_dir() else ""))
                if c.is_dir() and lvl + 1 < depth:
                    walk(c, lvl + 1)
        walk(p, 0)
        return ToolResult(True, f"{rel or '.'}: {len(out)} entries", "\n".join(out))

    async def search_files(self, pattern: str, regex: bool = False, glob: str = "") -> ToolResult:
        await asyncio.to_thread(self.p.index.refresh)
        hits = self.p.index.grep(pattern, 40, glob or None, regex)
        return ToolResult(True, f"{len(hits)} matches for {pattern!r}", "\n".join(f"{h.path}:{h.line}: {h.text}" for h in hits))

    async def search_symbols(self, query: str) -> ToolResult:
        await asyncio.to_thread(self.p.index.refresh)
        hits = self.p.index.search(query, 20)
        return ToolResult(True, f"{len(hits)} results", "\n".join(f"{h.kind:7} {h.text}  {h.path}:{h.line}" for h in hits))

    # ---- shell ----------------------------------------------------------------------
    async def _sh(self, cmd: str, timeout: float | None = None) -> CmdResult:
        return await run_command(cmd, self.p.root, timeout or self.timeout)

    async def terminal(self, command: str, timeout: int = 0) -> ToolResult:
        d = self.perms.check_shell(command)
        if not await self.perms.resolve(d, f"run: {command}"):
            return ToolResult(False, f"{d.reason} — not run", denied=True)
        r = await self._sh(command, timeout or None)
        cls = "" if r.ok else classify_failure(r.output)
        return ToolResult(r.ok, f"`{self._pretty(command)[:70]}` exit {r.code}" + (" (timed out)" if r.timed_out else ""), truncate(r.output, self.max_out),
                          failure_class=cls, data={"code": r.code})

    def _pretty(self, cmd: str) -> str:
        py = self.p.info.python
        return cmd.replace(shlex.quote(py), "python").replace(py, "python") if py else cmd

    async def git(self, args: str) -> ToolResult:
        sub = args.split()[0] if args.split() else ""
        if sub in ("push", "pull", "fetch", "clone", "remote", "reset", "clean", "rebase", "checkout", "restore"):
            return ToolResult(False, f"git {sub} is not available to the agent (external or destructive)", denied=True)
        if sub in ("add", "commit", "branch", "switch", "merge", "tag") and self.perms.mode == "safe":
            if not await self.perms.resolve(self.perms.check_shell("git " + args), f"git {args}"):
                return ToolResult(False, "not permitted in SAFE mode", denied=True)
        if not self.p.git.is_repo:
            return ToolResult(False, "not a git repository")
        r = await self._sh("git " + args)
        return ToolResult(r.ok, f"git {args[:60]}", truncate(r.output, self.max_out))

    # ---- quality gates ---------------------------------------------------------------
    async def _gate(self, kind: str, cmd: str, framework: str = "", extra: str = "") -> ToolResult:
        if not cmd:
            return ToolResult(False, f"no {kind} command detected for this project", data={"missing": True})
        full = cmd + (" " + extra if extra else "")
        d = self.perms.check_shell(full)
        if d.action == "ask" and self.perms.mode != "safe":
            d.action = "allow"                     # detected project gates are trusted
        if not await self.perms.resolve(d, f"run {kind}: {full}"):
            return ToolResult(False, "not permitted", denied=True)
        r = await self._sh(full)
        s = parse_tests(framework or self.p.info.test_framework, r) if kind == "tests" else None
        ok = s.ok if s else r.ok
        if kind == "tests" and s and s.total == 0 and r.ok:
            ok = True
        self.p.record_tests(kind, full, s or _simple(r), r.duration)
        summary = (f"{s.line()}" if s else f"{kind} {'pass' if ok else 'fail'}") + f" ({r.duration:.1f}s)"
        return ToolResult(ok, summary, truncate(r.output, self.max_out, keep_tail=True), failure_class="" if ok else classify_failure(r.output),
                          data={"passed": s.passed if s else int(ok), "failed": s.failed if s else int(not ok), "total": s.total if s else 1,
                                "failures": s.failures if s else [], "code": r.code})

    async def tests(self, args: str = "") -> ToolResult:
        extra = " ".join(shlex.quote(a) for a in shlex.split(args)) if args else ""
        return await self._gate("tests", self.p.info.test_cmd, self.p.info.test_framework, extra)

    async def build(self) -> ToolResult:
        return await self._gate("build", self.p.info.build_cmd)

    async def lint(self) -> ToolResult:
        return await self._gate("lint", self.p.info.lint_cmd)

    async def typecheck(self) -> ToolResult:
        return await self._gate("typecheck", self.p.info.typecheck_cmd)

    async def package_manager(self, action: str, packages: str = "") -> ToolResult:
        pm = (self.p.info.package_managers or [""])[0]
        pk = " ".join(shlex.quote(x) for x in packages.split())
        py = self.p.info.python or "python3"
        table = {
            ("npm", "install"): "npm install", ("npm", "add"): f"npm install {pk}", ("npm", "remove"): f"npm uninstall {pk}",
            ("pnpm", "install"): "pnpm install", ("pnpm", "add"): f"pnpm add {pk}", ("pnpm", "remove"): f"pnpm remove {pk}",
            ("yarn", "install"): "yarn install", ("yarn", "add"): f"yarn add {pk}", ("yarn", "remove"): f"yarn remove {pk}",
            ("pip", "install"): f"{py} -m pip install -r requirements.txt", ("pip", "add"): f"{py} -m pip install {pk}",
            ("uv", "install"): "uv sync", ("uv", "add"): f"uv add {pk}", ("uv", "remove"): f"uv remove {pk}",
            ("cargo", "add"): f"cargo add {pk}", ("cargo", "install"): "cargo fetch",
        }
        cmd = table.get((pm, action))
        if not cmd:
            return ToolResult(False, f"unsupported package action {action!r} for manager {pm or 'unknown'}")
        return await self.terminal(cmd)

    # ---- processes -------------------------------------------------------------------
    async def process_manager(self, action: str, name: str = "dev", command: str = "", port: int = 0) -> ToolResult:
        pm = self.p.procs
        if action == "list":
            rows = pm.list()
            return ToolResult(True, f"{len(rows)} processes", "\n".join(f"{r['name']:10} {r['status']:8} pid {r['pid']} {r['url']}" for r in rows))
        if action == "logs":
            return ToolResult(True, f"{name} log tail", truncate(pm.tail(name, 60), self.max_out))
        if action == "stop":
            return ToolResult(pm.stop(name), f"stopped {name}")
        if action in ("start", "restart"):
            info = self.p.info
            command = command or info.dev_cmd
            port = port or info.dev_port
            if not command:
                return ToolResult(False, "no dev command detected; pass `command`")
            d = self.perms.check_shell(command)
            if d.action == "ask" and self.perms.mode != "safe":
                d.action = "allow"
            if not await self.perms.resolve(d, f"start {name}: {command}"):
                return ToolResult(False, "not permitted", denied=True)
            try:
                r = await (pm.restart(name) if action == "restart" else pm.start(name, command, port))
            except RuntimeError as e:
                return ToolResult(False, str(e).splitlines()[0], str(e), failure_class=classify_failure(str(e)))
            return ToolResult(True, f"{name} {'already running' if r.get('reused') else 'started'} at {r.get('url') or 'n/a'}", data=r)
        return ToolResult(False, f"unknown action {action!r}")

    # ---- browser ---------------------------------------------------------------------
    async def browser(self, url: str, steps: list | None = None, audit: bool = False, viewport: str = "desktop") -> ToolResult:
        if not playwright_installed():
            return ToolResult(False, "Playwright is not installed", "pip install 'genius-dev[browser]' && playwright install chromium")
        r = await run_flow(url, steps, self.p.gdir / "screenshots", bool(self.p.cfg.get("browser.headless", True)), viewport, audit)
        out = "\n".join(["steps: " + " → ".join(r.log)] + ([f"error: {r.error}"] if r.error else [])
                        + [f"console: {c}" for c in r.console_errors[:8]] + [f"network: {c}" for c in r.failed_requests[:8]]
                        + [f"ui[{i['sev']}] {i['kind']}: {i['msg']}" for i in r.issues[:20]])
        return ToolResult(r.ok, r.summary(), out, data={"screenshots": r.screenshots, "issues": r.issues, "console": r.console_errors})

    async def screenshot(self, url: str, viewport: str = "desktop") -> ToolResult:
        r = await self.browser(url, [], False, viewport)
        return ToolResult(r.ok, "screenshot " + (r.data.get("screenshots") or ["failed"])[-1], r.output, data=r.data)

    # ---- data/network/docs ------------------------------------------------------------
    async def database(self, path: str, sql: str) -> ToolResult:
        p, rel = self._rel(path)
        if not p.is_file() or not self.perms.inside_project(p):
            return ToolResult(False, f"{rel} not found in project")
        read_only = bool(re.match(r"\s*(select|pragma|explain|with)\b", sql, re.I))
        if not read_only:
            d = self.perms.check_write(rel, "database write")
            d.action = "ask"
            d.reason = "modifying a database"
            if not await self.perms.resolve(d, f"SQL on {rel}: {sql[:80]}"):
                return ToolResult(False, "write query not permitted", denied=True)
        con = sqlite3.connect(f"file:{p}?mode={'ro' if read_only else 'rw'}", uri=True)
        try:
            cur = con.execute(sql)
            rows = cur.fetchmany(50)
            cols = [c[0] for c in cur.description] if cur.description else []
            con.commit()
        finally:
            con.close()
        return ToolResult(True, f"{len(rows)} rows", "\n".join([" | ".join(cols)] + [" | ".join(map(str, r)) for r in rows]))

    async def http(self, url: str, method: str = "GET", body: str = "") -> ToolResult:
        import httpx
        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
        if host not in ("localhost", "127.0.0.1", "0.0.0.0"):
            d = self.perms.check_shell("curl " + shlex.quote(url))
            d.action, d.reason = "ask", f"external request to {host}"
            if not await self.perms.resolve(d, f"HTTP {method} {url}"):
                return ToolResult(False, "external request not permitted", denied=True)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.request(method.upper(), url, content=body or None)
        except Exception as e:  # noqa: BLE001
            return ToolResult(False, f"request failed: {type(e).__name__}", failure_class="tool_unavailable")
        return ToolResult(r.status_code < 400, f"{method.upper()} {url} → {r.status_code}", truncate(r.text, self.max_out))

    async def docs_lookup(self, query: str) -> ToolResult:
        await asyncio.to_thread(self.p.index.refresh)
        hits = [h for h in self.p.index.grep(query, 60) if h.path.lower().endswith(".md")][:15]
        return ToolResult(True, f"{len(hits)} doc matches", "\n".join(f"{h.path}:{h.line}: {h.text}" for h in hits))

    async def project_index(self, query: str) -> ToolResult:
        await asyncio.to_thread(self.p.index.refresh)
        ix = self.p.index
        q = query.strip()
        if q.startswith("routes"):
            return ToolResult(True, "routes", "\n".join(f"{r['method']:6} {r['route']}  {r['path']}:{r['line']}" for r in ix.routes()))
        if q.startswith("tables"):
            return ToolResult(True, "tables", "\n".join(f"{t['name']}  {t['path']}:{t['line']}" for t in ix.tables()))
        if q.startswith("symbols "):
            return ToolResult(True, "symbols", "\n".join(f"{s['line']:4} {s['kind']:9} {s['name']}{s['sig']}" for s in ix.symbols(q[8:].strip())))
        if q.startswith("relevant "):
            return ToolResult(True, "relevant files", "\n".join(f"{p}  ({s:.0f})" for p, s in ix.relevant_files(q[9:])))
        return ToolResult(True, "index stats", json.dumps(ix.stats()))

    async def logs(self, name: str = "", lines: int = 40) -> ToolResult:
        if name:
            return ToolResult(True, f"{name} log", truncate(self.p.procs.tail(name, lines), self.max_out))
        rows = self.p.store.query("SELECT ts,category,label,message FROM events ORDER BY id DESC LIMIT ?", (lines,))
        return ToolResult(True, f"{len(rows)} events", "\n".join(f"{time.strftime('%H:%M:%S', time.localtime(r['ts']))} {r['label']:8} {r['message']}" for r in reversed(rows)))

    async def env_inspect(self) -> ToolResult:
        tools = {}
        for t, arg in (("python3", "--version"), ("node", "--version"), ("npm", "--version"), ("git", "--version"), ("cargo", "--version"),
                       ("go", "version"), ("docker", "--version"), ("uv", "--version")):
            if shutil.which(t):
                r = await run_command(f"{t} {arg}", self.p.root, 10)
                tools[t] = r.output.splitlines()[0] if r.output else "?"
        import os
        names = sorted(k for k in os.environ if not any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASS")))
        return ToolResult(True, f"{len(tools)} tools detected", json.dumps({"tools": tools, "env_var_names": names[:60]}, indent=1))


def _simple(r: CmdResult):
    from .runner import TestSummary
    return TestSummary(int(r.ok), int(not r.ok), 1, [] if r.ok else [truncate(r.output, 200)], r.ok)
