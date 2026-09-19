"""genius doctor — every check performs the thing it reports on (runs the binary, opens the DB, launches the browser, runs the tests)."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .plugins import Check, load_registry
from .project import Project
from .router import Router
from .runner import run_command
from .secrets import SERVICE


@dataclass
class DoctorReport:
    sections: dict[str, list[Check]] = field(default_factory=dict)
    duration: float = 0.0

    @property
    def problems(self) -> list[Check]:
        return [c for cs in self.sections.values() for c in cs if c.status in ("warn", "fail")]

    def to_dict(self) -> dict[str, Any]:
        return {"sections": {k: [c.__dict__ for c in v] for k, v in self.sections.items()}, "problems": len(self.problems), "duration": round(self.duration, 2)}


async def check_system(project: Project | None) -> list[Check]:
    """Runs each plugin's real doctor checks (python, node/npm, git, rust/cargo/go, xcode/android, docker) plus uv and ollama."""
    reg = load_registry(project.root if project else None)
    results = await asyncio.gather(*[p.doctor(project) for p in reg.plugins if p.name != "playwright"], return_exceptions=True)
    out: list[Check] = []
    for p, res in zip([p for p in reg.plugins if p.name != "playwright"], results):
        if isinstance(res, BaseException):
            out.append(Check(p.name, "fail", f"plugin doctor crashed: {res}"))
        else:
            out += res
    import shutil
    for name, binary, cmd in (("uv", "uv", "uv --version"), ("Ollama", "ollama", "ollama --version")):
        if shutil.which(binary):
            r = await run_command(cmd, Path(os.getcwd()), 10)
            out.append(Check(name, "ok" if r.ok else "fail", (r.output.splitlines() or [""])[0][:60]))
        else:
            out.append(Check(name, "skip", "not installed"))
    for e in reg.errors:
        out.append(Check("Plugin", "warn", e[:100]))
    return out


async def check_genius(project: Project | None) -> list[Check]:
    from .config import global_dir
    out: list[Check] = []
    d = global_dir()
    try:
        with tempfile.NamedTemporaryFile(dir=d, delete=True) as f:
            f.write(b"x")
        out.append(Check("Config dir", "ok", f"{d} (writable)"))
    except OSError as e:
        out.append(Check("Config dir", "fail", f"{d} not writable: {e}"))
    try:
        import keyring
        keyring.get_password(SERVICE, "__genius_doctor__")                      # real backend round-trip; absent entry -> None
        out.append(Check("Keychain", "ok", type(keyring.get_keyring()).__module__.split(".")[-1] + " backend reachable"))
    except Exception as e:  # noqa: BLE001
        out.append(Check("Keychain", "warn", f"unavailable ({type(e).__name__}) — use environment variables for keys"))
    from .browser import browser_ready, playwright_installed
    if not playwright_installed():
        out.append(Check("Playwright", "warn", "not installed — pip install 'genius-dev[browser]' && playwright install chromium"))
    else:
        ok, detail = await browser_ready()
        out.append(Check("Playwright", "ok", "installed"))
        out.append(Check("Browser (Chromium)", "ok" if ok else "warn", "launched and closed" if ok else detail))
    return out


def _db_checks(store, label: str) -> list[Check]:
    out: list[Check] = []
    try:
        row = store.conn.execute("PRAGMA integrity_check").fetchone()
        from .db import MIGRATIONS
        out.append(Check(label, "ok" if row[0] == "ok" else "fail", f"integrity {row[0]} · schema v{store.version}/{len(MIGRATIONS)}"))
        if store.version != len(MIGRATIONS):
            out.append(Check(label + " migrations", "warn", "schema out of date", "migrate"))
    except sqlite3.Error as e:
        out.append(Check(label, "fail", str(e)))
    return out


def check_project(p: Project) -> list[Check]:
    out: list[Check] = []
    info = p.info
    if not p.initialized:
        out.append(Check("Memory (.genius)", "warn", "missing", "init"))
        return out
    try:
        probe = p.gdir / ".doctor-write-test"
        probe.write_text("x"); probe.unlink()
        out.append(Check("Memory (.genius)", "ok", f"{sum(1 for _ in p.gdir.iterdir())} entries, writable"))
    except OSError as e:
        out.append(Check("Memory (.genius)", "fail", str(e)))
    out += _db_checks(p.store, "Memory database")
    try:
        t = time.perf_counter()
        p.index.refresh()
        st = p.index.stats()
        on_disk = sum(1 for _ in p.index.walk())
        ok = st["files"] == on_disk
        out.append(Check("Project index", "ok" if ok else "warn", f"{st['files']} files · {st['symbols']} symbols · {st['routes']} routes · refreshed in {time.perf_counter() - t:.2f}s" + ("" if ok else f" (disk has {on_disk})")))
        chunks = p.index.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        out.append(Check("Semantic index", "ok" if chunks or st["files"] == 0 else "warn", f"{chunks} retrieval chunks (offline BM25 + concepts)"))
    except Exception as e:  # noqa: BLE001
        out.append(Check("Project index", "fail", f"{type(e).__name__}: {e}"))
    out.append(Check("Frontend", "ok" if info.structure.get("frontend-ish") else "skip", ", ".join(info.structure.get("frontend-ish", [])) or "none detected"))
    out.append(Check("Backend", "ok" if info.structure.get("backend-ish") else "skip", ", ".join(info.structure.get("backend-ish", [])) or "none detected"))
    out.append(Check("Database", "ok" if info.databases else "skip", ", ".join(info.databases) or "none detected"))
    out.append(Check("Test command", "ok" if info.test_cmd else "warn", p.pretty(info.test_cmd) or "none detected"))
    out.append(Check("Build command", "ok" if info.build_cmd else "skip", p.pretty(info.build_cmd) or "none detected"))
    stale = [x for x in p.procs.list() if x["status"] == "exited"]
    if stale:
        out.append(Check("Stale processes", "warn", ", ".join(x["name"] for x in stale), "prune_procs"))
    running = [x for x in p.procs.list() if x["status"] == "running"]
    if running:
        out.append(Check("Running processes", "ok", ", ".join(f"{x['name']}:{x['port'] or x['pid']}" for x in running)))
    return out


async def check_models(project: Project | None, router: Router) -> list[Check]:
    from .providers.status import statuses
    from .cli_probe import probe_providers
    probes = await probe_providers(router, record=True)
    out: list[Check] = []
    for s in statuses(router.cfg, router.gstore, probes):
        if s.name == "mock":
            out.append(await _mock_check(router, s))
            continue
        status = {"CONNECTED": "ok", "LIVE VERIFIED": "ok", "ERROR": "fail", "NEEDS MODEL": "warn", "KEY SET · UNTESTED": "warn", "UNTESTED": "warn"}.get(s.state, "skip")
        detail = s.state + (f" · {s.latency_ms}ms" if s.latency_ms is not None else "") + (f" — {s.detail}" if s.detail and s.state not in ("READY FOR KEY",) else "")
        if s.state == "READY FOR KEY":
            detail = "READY FOR KEY · implemented, mock-tested, not contacted"
        out.append(Check(s.label, status, detail))
    return out


async def _mock_check(router: Router, s) -> Check:
    if not s.configured:
        return Check("Mock provider", "warn", "not configured — run `genius models add mock`")
    from .agent import extract_json
    prov = router.provider("mock")
    t = time.perf_counter()
    try:
        c = await prov.generate([{"role": "user", "content": "GOAL: doctor check"}], "[[role:planner]]", json_mode=True)
        plan = extract_json(c.text) or {}
        ok = bool(plan.get("requirements"))
        return Check("Mock provider", "ok" if ok else "fail", f"planner round-trip {(time.perf_counter() - t) * 1000:.0f}ms · protocol JSON {'valid' if ok else 'INVALID'}")
    except Exception as e:  # noqa: BLE001
        return Check("Mock provider", "fail", f"{type(e).__name__}: {e}")


async def check_quality(p: Project, quick: bool) -> list[Check]:
    """Runs the project's real lint / typecheck / build / tests (skip with --quick, which reports the last recorded runs)."""
    from .permissions import Permissions
    from .events import EventBus
    from .tools import ToolBox
    out: list[Check] = []
    tb = ToolBox(p, Permissions("standard", p.root), EventBus())
    for label, kind, cmd in (("Lint", "lint", p.info.lint_cmd), ("Type check", "typecheck", p.info.typecheck_cmd), ("Build", "build", p.info.build_cmd), ("Unit tests", "tests", p.info.test_cmd)):
        if quick or not cmd:
            r = p.last_run(kind)
            if r:
                age = int((time.time() - r["ts"]) / 60)
                d = f"{r['passed']}/{r['total']}" if kind == "tests" else ("PASS" if r["ok"] else "FAIL")
                out.append(Check(label, "ok" if r["ok"] else "fail", f"{d} · last run {age}m ago (not re-run)"))
            else:
                out.append(Check(label, "skip", "no command detected" if not cmd else "not run (--quick)"))
            continue
        res = await tb.execute(kind, {})
        out.append(Check(label, "ok" if res.ok else "fail", f"{res.summary}" + ("" if res.ok else " — " + (res.output.strip().splitlines() or [""])[-1][:70])))
    return out


async def run_doctor(project: Project | None, router: Router | None, quick: bool = False) -> DoctorReport:
    t = time.perf_counter()
    rep = DoctorReport()
    sysc, gen = await asyncio.gather(check_system(project), check_genius(project))
    rep.sections["SYSTEM"] = sysc
    rep.sections["GENIUS"] = gen + ([c for c in _db_checks(router.gstore, "Global database")] if router else [])
    if project:
        rep.sections["PROJECT"] = check_project(project)
    if router:
        rep.sections["MODELS"] = await check_models(project, router)
    if project and project.initialized:
        rep.sections["QUALITY"] = await check_quality(project, quick)
    rep.duration = time.perf_counter() - t
    return rep


def apply_fixes(project: Project, rep: DoctorReport) -> list[str]:
    """Safe, reversible repairs only. Never installs software or touches user code."""
    done: list[str] = []
    for c in rep.problems:
        if c.fix == "init":
            project.init()
            done.append("initialised .genius/ project memory")
        elif c.fix == "git_init" and project.cfg.get("git.auto_init", True):
            project.git.init()
            done.append("initialised git repository (checkpoints enabled)")
        elif c.fix == "prune_procs":
            project.store.execute("DELETE FROM processes WHERE status IN ('exited','stopped')")
            done.append("removed stale process records")
        elif c.fix == "migrate":
            project.store.migrate()
            done.append("migrated the project database")
    project.store.migrate()
    project.sync_memory()
    if project.git.is_repo:
        project.git.exclude(".genius/")
    return done
