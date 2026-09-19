"""Command-line entry point. `genius` with no arguments opens the TUI."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__, theme as T
from .config import PRESETS, Config, ProviderConfig, dumps_toml
from .runtime import Runtime, build_runtime
from .secrets import keychain_set, redact

app = typer.Typer(add_completion=True, no_args_is_help=False, invoke_without_command=True, pretty_exceptions_enable=False,
                  help="Genius Dev — autonomous AI software engineering, in your terminal.", rich_markup_mode=None)
models_app = typer.Typer(invoke_without_command=True, help="List, add and test model providers.", pretty_exceptions_enable=False)
app.add_typer(models_app, name="models")
con = Console(highlight=False)
err = Console(stderr=True, highlight=False)

PathOpt = typer.Option(".", "--path", "-C", help="Project directory")
JsonOpt = typer.Option(False, "--json", help="Machine-readable output")


def _run(coro):
    return asyncio.run(coro)


def _rt(path: str = ".", permission: str | None = None, local: bool = False, ask=None) -> Runtime:
    from .project import UnsafeRoot
    try:
        return build_runtime(path, ask=ask, permission=permission, mode="local_only" if local else None, seed=True)
    except UnsafeRoot as e:
        err.print(Text(f"✕ {e}", style=T.FAIL))
        raise typer.Exit(2)


def _emit(obj: Any, as_json: bool, render=None) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    elif render:
        render()


def _complete_provider(incomplete: str) -> list[str]:
    try:
        return [n for n in Config(None).providers() if n.startswith(incomplete)]
    except Exception:  # noqa: BLE001
        return []


def _complete_preset(incomplete: str) -> list[str]:
    return [k for k in PRESETS if k.startswith(incomplete)]


def _complete_checkpoint(incomplete: str) -> list[str]:
    try:
        from .project import Project
        p = Project.open(".", create=False)
        if not p.initialized:
            return []
        p.init()
        return [c["id"] for c in p.checkpoints.list() if c["id"].startswith(incomplete)]
    except Exception:  # noqa: BLE001
        return []


def _head(title: str) -> None:
    con.print(Text(title.upper(), style=f"bold {T.DIM}"))


@app.callback()
def _main(ctx: typer.Context, version: bool = typer.Option(False, "--version", help="Show version"),
          local: bool = typer.Option(False, "--local", help="LOCAL ONLY: use Ollama / LM Studio / local endpoints"),
          path: str = PathOpt) -> None:
    if version:
        print(f"genius-dev {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        if sys.stdin.isatty() and not Config(None).get("general.onboarded"):
            _onboard()
        from .project import UnsafeRoot
        from .tui.app import run_tui
        try:
            run_tui(path, local)
        except UnsafeRoot as e:
            err.print(Text(f"✕ {e}", style=T.FAIL))
            raise typer.Exit(2)


# ---------------------------------------------------------------------------------------------
@app.command()
def tui(path: str = PathOpt, local: bool = typer.Option(False, "--local")) -> None:
    """Open the interactive TUI."""
    from .tui.app import run_tui
    run_tui(path, local)


@app.command()
def init() -> None:
    """Run the first-run setup wizard."""
    _onboard()


def _onboard() -> None:
    """First run. The offline mock provider is always set up first, so the app is usable immediately with no account."""
    from .config import seed_default
    cfg = Config(None)
    seed_default(cfg)
    con.print()
    con.print(Text("GENIUS DEV", style=f"bold {T.TEXT}"), Text(" — setup", style=T.MUTED))
    con.print(Text("You can use Genius Dev right now with the built-in offline model (try `genius demo`). Every step below is optional; Ctrl-C skips.\n", style=T.MUTED))
    try:
        _head("1 · Add a provider (optional — keys can be added any time with `genius models key <name>`)")
        names = ["none — stay offline for now"] + [n for n in PRESETS if n != "mock"]
        for i, n in enumerate(names):
            con.print(f"  {i}  {n}")
        pick = typer.prompt("Choose", default="0", show_default=True)
        primary = names[int(pick)] if pick.isdigit() and 0 < int(pick) < len(names) else ""
        if primary:
            preset = {**PRESETS[primary]}
            cfg.save_provider(ProviderConfig.from_dict(primary, preset))
            if preset["api_key_ref"] != "none" and typer.confirm(f"Store the {primary} key in the macOS Keychain now? (you can also do it later)", default=False):
                key = typer.prompt("API key (hidden)", hide_input=True)
                if key and keychain_set(primary, key):
                    p = cfg.providers()[primary]
                    p.api_key_ref = f"keychain:{primary}"
                    cfg.save_provider(p)
                    con.print(Text(f"  {primary}: Configured: YES", style=T.OK))
                else:
                    con.print(Text("  Keychain unavailable or empty key — nothing stored.", style=T.WARN))
            model = typer.prompt(f"Model ID for {primary} (blank = choose later)", default="", show_default=False)
            if model:
                p = cfg.providers()[primary]
                p.model = model
                cfg.save_provider(p)
            if typer.confirm(f"Use {primary} as the primary model?", default=False):
                cfg.set("general.primary", primary)
        _head("2 · Permission level")
        cfg.set("general.permission", typer.prompt("safe / standard / autonomous", default="standard"))
        _head("3 · Daily API budget")
        cfg.set("general.daily_budget", float(typer.prompt("USD per day (0 = unlimited)", default="0")))
        _head("4 · Browser")
        cfg.set("browser.headless", typer.confirm("Run browser tests headless?", default=True))
    except (typer.Abort, KeyboardInterrupt):
        con.print(Text("\nSkipped — change anything later in Settings or with `genius config` / `genius models`.", style=T.MUTED))
    cfg.set("general.onboarded", True)
    con.print(Text("\nDone. Try `genius demo`, then `genius doctor`. Add a provider whenever you like: `genius models`.\n", style=T.MUTED))


# ---------------------------------------------------------------------------------------------
@app.command()
def new(name: str = typer.Argument(None), kind: str = typer.Option(None, "--type", "-t", help="web|desktop|android|ios|api|cli|agent|extension|wordpress|game|audio|custom")) -> None:
    """Create a project with sensible defaults."""
    kinds = ["Web App", "Desktop App", "Android App", "iOS App", "API", "CLI", "AI Agent", "Chrome Extension", "WordPress Plugin", "Game", "Audio Application", "Custom"]
    if kind is None:
        for i, k in enumerate(kinds, 1):
            con.print(f"  {i:>2}  {k}")
        pick = typer.prompt("Type", default="1")
        kind = kinds[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(kinds) else pick
    name = name or typer.prompt("Project name")
    root = Path(name).resolve()
    root.mkdir(parents=True, exist_ok=True)
    from .project import Project
    p = Project.open(root)
    if not p.git.is_repo:
        p.git.init()
    p.git.exclude(".genius/")
    p.append_mem("decisions.md", f"- Project type: {kind}")
    (p.gdir / "architecture.md").write_text(f"# Architecture\n\nProject type: **{kind}**. Stack to be decided by the planner.\n")
    con.print(f"Created {root}. Next:  cd {name} && genius   (then describe what to build)")


@app.command("open")
def open_(path: str = typer.Argument("."), json_out: bool = JsonOpt) -> None:
    """Inspect an existing repository and create project memory. Nothing is modified besides .genius/."""
    from .project import Project
    p = Project.open(path)
    info = p.redetect()
    p.index.refresh(force=True)
    p.sync_memory()
    d = {**info.to_dict(), "index": p.index.stats(), "root": str(p.root), "git": p.git.is_repo}
    def render():
        _head(p.name)
        for k, v in (("kind", info.kind), ("languages", info.languages), ("frameworks", info.frameworks), ("package managers", info.package_managers),
                     ("databases", info.databases), ("services", info.services), ("tests", info.test_cmd or "—"), ("build", info.build_cmd or "—"),
                     ("dev server", info.dev_cmd or "—"), ("git", "yes" if p.git.is_repo else "no (checkpoints will init one)")):
            con.print(f"  {k:18}{', '.join(v) if isinstance(v, list) else v}")
        con.print(f"  {'index':18}{p.index.stats()['files']} files · {p.index.stats()['symbols']} symbols · {p.index.stats()['routes']} routes")
        con.print(Text("\n  Project memory created in .genius/. Run `genius` to start working.", style=T.MUTED))
    _emit(d, json_out, render)


@app.command()
def status(path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """Project status at a glance."""
    from .status import snapshot
    rt = _rt(path)
    s = snapshot(rt)
    def render():
        con.print(Text.assemble((s["project"], f"bold {T.TEXT}"), (f"  {s['permission'].upper()}  ·  {s['routing_mode']}", T.MUTED)))
        con.print(T.bar(s["progress"], 40), Text(f"  {s['progress']:.0f}%", style=T.TEXT))
        rq = s["requirements"]
        con.print(Text(f"  {rq['PASS']} pass · {rq['FAIL']} fail · {rq['BLOCKED']} blocked · {rq['NOT_STARTED'] + rq['IN_PROGRESS']} open", style=T.MUTED))
        t = s["tests"]
        tests_txt = f"{t['passed']}/{t['total']}" if t else "—"
        build_txt = ("PASS" if s["build"] else "FAIL") if s["build"] is not None else "—"
        con.print(f"\n  TESTS   {tests_txt:10} BUILD  {build_txt}")
        con.print(f"  MODEL   {s['model'] or 'none configured':10} COST   {T.money(s['cost_today'])} today" + (f" / {T.money(s['budget'])}" if s["budget"] else ""))
    _emit(s, json_out, render)


@app.command()
def resume(path: str = PathOpt, no_continue: bool = typer.Option(False, "--no-continue", help="Show state only")) -> None:
    """Reconstruct the previous session and continue."""
    from .status import resume_text, resume_view
    rt = _rt(path, ask=_cli_ask)
    v = resume_view(rt)
    con.print(resume_text(v))
    open_n = v["requirements"]["FAIL"] + v["requirements"]["NOT_STARTED"] + v["requirements"]["IN_PROGRESS"]
    if no_continue or not open_n:
        con.print(Text("\nNothing outstanding." if not open_n else "", style=T.MUTED))
        return
    con.print(Text("\nContinuing…\n", style=T.MUTED))
    rt.bus.subscribe(_print_event)
    rep = _run(rt.agent.run(""))
    con.print("\n" + rep.text())


async def _cli_ask(title: str, reason: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return typer.confirm(f"\n  Permission needed: {title}\n  {reason}. Allow?", default=False)


def _print_event(ev) -> None:
    if ev.category == "MODEL" and ev.label == "ROUTE":
        return
    color = {"ERRORS": T.FAIL, "TESTS": T.ACCENT, "BUILD": T.ACCENT}.get(ev.category, T.MUTED)
    room = max(20, con.width - 22)
    con.print(Text.assemble((f"{ev.clock}  ", T.DIM), (f"{ev.label:10}", color), (ev.message.replace("\n", " ")[:room], T.TEXT), no_wrap=True, overflow="ellipsis"))


@app.command()
def plan(goal: str = typer.Argument(...), path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """Analyse and propose a plan without modifying anything."""
    rt = _rt(path)
    pl = _run(rt.agent.plan(goal))
    d = {"goal": goal, "requirements": pl.requirements, "tasks": pl.tasks, "files": pl.files_likely, "risks": pl.risks, "tests": pl.tests}
    def render():
        _head("plan · nothing modified")
        for r in pl.requirements:
            con.print(f"  ○ {r['description']}  ", Text(f"[{r.get('verify', '?')}]", style=T.DIM))
        con.print()
        _head("files likely affected")
        for f in pl.files_likely:
            con.print(f"  {f}")
        if pl.risks:
            _head("risks")
            for r in pl.risks:
                con.print(Text(f"  ! {r}", style=T.WARN))
        con.print(Text(f"\n  Execute with:  genius run {json.dumps(goal)}", style=T.MUTED))
    _emit(d, json_out, render)


@app.command()
def run(goal: str = typer.Argument("", help="What to build or fix. Empty = continue open requirements."), path: str = PathOpt,
        headless: bool = typer.Option(False, "--headless", help="No TUI; stream progress to stdout"),
        json_out: bool = JsonOpt, permission: str = typer.Option(None, "--permission", "-p", help="safe|standard|autonomous"),
        local: bool = typer.Option(False, "--local"), budget: float = typer.Option(None, "--budget", help="Daily USD cap for this run")) -> None:
    """Run the autonomous loop on a goal."""
    if not headless and not json_out and sys.stdout.isatty():
        from .tui.app import run_tui
        run_tui(path, local, initial=goal or "continue", permission=permission)
        return
    rt = _rt(path, permission=permission, local=local, ask=None if not sys.stdin.isatty() else _cli_ask)
    if budget is not None:
        rt.project.cfg.set("general.daily_budget", budget)
    if json_out:
        rt.bus.subscribe(lambda ev: print(ev.to_json(), flush=True))
    else:
        rt.bus.subscribe(_print_event)
    rep = _run(rt.agent.run(goal))
    if json_out:
        print(json.dumps({"type": "result", **rep.to_dict()}, default=str), flush=True)
    else:
        con.print("\n" + rep.text())
    raise typer.Exit(0 if rep.success else 1)


@app.command()
def finish(path: str = PathOpt, no_fix: bool = typer.Option(False, "--no-fix", help="Audit only"), json_out: bool = JsonOpt,
           permission: str = typer.Option(None, "--permission", "-p")) -> None:
    """Deep completion audit — repairs what it safely can, repeats until PASS or a real blocker."""
    from .audit import run_finish
    from .tools import ToolBox
    rt = _rt(path, permission=permission, ask=_cli_ask)
    if not json_out:
        rt.bus.subscribe(_print_event)
    tools = ToolBox(rt.project, rt.perms, rt.bus)
    rep = _run(run_finish(rt.project, tools, rt.router, rt.bus, rt.agent, fix=not no_fix))
    _emit(rep.to_dict(), json_out, lambda: con.print("\n" + rep.text()))
    raise typer.Exit(0 if rep.passed else 1)


@app.command()
def doctor(path: str = PathOpt, fix: bool = typer.Option(False, "--fix", help="Repair safe problems automatically"), json_out: bool = JsonOpt,
           quick: bool = typer.Option(False, "--quick", help="Don't re-run build/tests (report last recorded results)")) -> None:
    """Check system, project, models and quality."""
    from .doctor import apply_fixes, run_doctor
    rt = _rt(path)
    rep = _run(run_doctor(rt.project, rt.router, quick))
    fixed = apply_fixes(rt.project, rep) if fix else []
    if fix:
        rep = _run(run_doctor(rt.project, rt.router, True))
    d = {**rep.to_dict(), "fixed": fixed}
    def render():
        con.print(Text("GENIUS DEV DOCTOR", style=f"bold {T.TEXT}"))
        for sec, cs in rep.sections.items():
            con.print()
            _head(sec)
            for c in cs:
                g, col = {"ok": ("✓", T.OK), "fail": ("✕", T.FAIL), "warn": ("!", T.WARN), "skip": ("·", T.DIM)}[c.status]
                con.print(Text.assemble(("  ", ""), (g + " ", col), (f"{c.name:24}", T.TEXT), (c.detail, T.MUTED)))
        con.print()
        _head("problems")
        if fixed:
            for f in fixed:
                con.print(Text(f"  ✓ fixed: {f}", style=T.OK))
        pr = rep.problems
        con.print(Text(f"  {len(pr)} detected" if pr else "  none", style=T.WARN if pr else T.OK))
        for c in pr:
            con.print(Text(f"  · {c.name}: {c.detail}" + ("   (auto-fixable: genius doctor --fix)" if c.fix else ""), style=T.MUTED))
    _emit(d, json_out, render)
    raise typer.Exit(1 if any(c.status == "fail" for c in rep.problems) else 0)


def _gate(kind: str, path: str, json_out: bool) -> None:
    from .tools import ToolBox
    rt = _rt(path, permission="standard", ask=_cli_ask)
    tb = ToolBox(rt.project, rt.perms, rt.bus)
    r = _run(tb.execute(kind, {}))
    d = {"ok": r.ok, "summary": r.summary, **r.data}
    _emit(d, json_out, lambda: (con.print(Text(("✓ " if r.ok else "✕ ") + f"{kind.upper()}  {r.summary}", style=T.OK if r.ok else T.FAIL)), None if r.ok else con.print(r.output[-2500:])))
    raise typer.Exit(0 if r.ok else 1)


@app.command()
def test(path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """Run the project's tests."""
    _gate("tests", path, json_out)


@app.command()
def build(path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """Run the project's build."""
    _gate("build", path, json_out)


@app.command()
def preview(path: str = PathOpt, json_out: bool = JsonOpt, open_browser: bool = typer.Option(False, "--open", help="Open in the default browser"),
            no_check: bool = typer.Option(False, "--no-check", help="Skip the browser error check"), stop_: bool = typer.Option(False, "--stop", help="Stop the preview instead")) -> None:
    """Run the app the best way available: dev server (health + browser errors), desktop app, CLI, iOS Simulator, Android emulator, Compose."""
    from .preview import choose_plan, run_preview
    rt = _rt(path, permission="standard")
    if stop_:
        con.print(f"  stopped {rt.project.procs.stop_all()} process(es)"); return
    plan = choose_plan(rt.project)
    r = _run(run_preview(rt.project, open_browser, not no_check))
    def render():
        _head("preview · " + (r.title or plan.title))
        if r.reason and not r.ok and not r.process:
            con.print(Text(f"  ✕ cannot preview — {r.reason}", style=T.WARN)); return
        rows = [("KIND", r.kind), ("PROCESS", r.process or "—"), ("PORT", r.port or "—"), ("URL", r.url or "—"), ("STATUS", r.status or ("ok" if r.ok else "failed")), ("LOG", r.log or "—")]
        for k, v in rows:
            con.print(Text.assemble((f"  {k:9}", T.DIM), (str(v), T.ACCENT if k == "URL" and v != "—" else T.TEXT)))
        if r.health:
            con.print(Text.assemble(("  HEALTH   ", T.DIM), (f"HTTP {r.health.get('status')} in {r.health.get('ms')}ms" if r.health.get("ok") else "unhealthy", T.OK if r.health.get("ok") else T.FAIL)))
        b = r.browser
        if b.get("ran"):
            con.print(Text.assemble(("  BROWSER  ", T.DIM), ("no console errors" if not b["console_errors"] and not b["failed_requests"] else f"{len(b['console_errors'])} console error(s), {len(b['failed_requests'])} failed request(s)", T.OK if b["ok"] else T.WARN)))
            for e in (b["console_errors"] + b["failed_requests"])[:5]:
                con.print(Text("           " + e[:90], style=T.DIM))
        elif b:
            con.print(Text(f"  BROWSER  skipped — {b.get('reason')}", style=T.DIM))
        for st in r.steps:
            con.print(Text.assemble(("  ✓ " if st["ok"] else "  ✕ ", T.OK if st["ok"] else T.FAIL), (st["step"], T.TEXT), (f"  {st['detail'][:70]}" if st["detail"] and not st["ok"] else "", T.DIM)))
        con.print(Text(f"\n  {r.message}" if r.message else "", style=T.OK if r.ok else T.FAIL))
        for n in r.notes:
            con.print(Text("  · " + n.splitlines()[0], style=T.DIM))
    _emit(r.to_dict(), json_out, render)
    raise typer.Exit(0 if r.ok else 1)


@app.command()
def processes(path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """List managed dev processes."""
    rt = _rt(path)
    rows = rt.project.procs.list()
    _emit(rows, json_out, lambda: [con.print(f"  {r['name']:10}{r['status']:9}pid {r['pid']:<7}{r['url'] or '':24}{r['log']}") for r in rows] or con.print("  none"))


@app.command()
def stop(name: str = typer.Argument(None, help="Process name (omit = all)"), path: str = PathOpt) -> None:
    """Stop managed processes."""
    rt = _rt(path)
    n = int(rt.project.procs.stop(name)) if name else rt.project.procs.stop_all()
    con.print(f"  stopped {n} process(es)")


@app.command()
def restart(name: str = typer.Argument("dev"), path: str = PathOpt) -> None:
    """Restart a managed process."""
    rt = _rt(path)
    r = _run(rt.project.procs.restart(name))
    con.print(f"  {r['name']} → {r['url'] or 'running'}")


@app.command()
def logs(path: str = PathOpt, category: str = typer.Option("ALL", "--category", "-c", help="ALL|AGENT|TOOLS|BUILD|TESTS|BROWSER|MODEL|ERRORS"),
         n: int = typer.Option(40, "-n"), process: str = typer.Option(None, "--process", help="Show a process log"), json_out: bool = JsonOpt) -> None:
    """Show the event log (or a process log)."""
    rt = _rt(path)
    if process:
        con.print(rt.project.procs.tail(process, n))
        return
    q, args = "SELECT ts,category,label,message FROM events", []
    if category.upper() != "ALL":
        q += " WHERE category=?"; args.append(category.upper())
    rows = [dict(r) for r in rt.project.store.query(q + " ORDER BY id DESC LIMIT ?", (*args, n))][::-1]
    _emit(rows, json_out, lambda: [con.print(Text.assemble((__import__("time").strftime("%H:%M:%S", __import__("time").localtime(r["ts"])) + "  ", T.DIM), (f"{r['label']:10}", T.MUTED), (r["message"], T.TEXT))) for r in rows])


@app.command()
def diff(path: str = PathOpt, stat: bool = typer.Option(False, "--stat")) -> None:
    """Show what changed since the last commit."""
    rt = _rt(path)
    if not rt.project.git.is_repo:
        err.print("not a git repository"); raise typer.Exit(1)
    from .tui.widgets import render_diff
    if stat:
        for f, a, d in rt.project.git.numstat():
            con.print(Text.assemble((f"  {f:50}", T.TEXT), (f"+{a}", T.OK), ("  ", ""), (f"-{d}", T.FAIL)))
    else:
        con.print(render_diff(rt.project.git.diff()))


@app.command()
def undo(path: str = PathOpt) -> None:
    """Return to the last checkpoint (the undo is itself checkpointed)."""
    rt = _rt(path)
    try:
        r = rt.project.checkpoints.undo()
    except Exception as e:  # noqa: BLE001
        err.print(Text(f"✕ {e}", style=T.FAIL)); raise typer.Exit(1)
    con.print(f"  ✓ restored {r['checkpoint']}: {len(r['restored'])} file(s) restored, {len(r['deleted'])} removed.  Redo: genius restore {r['safety_checkpoint']}")


@app.command()
def checkpoint(message: str = typer.Argument("manual checkpoint"), path: str = PathOpt) -> None:
    """Create a checkpoint of the working tree."""
    rt = _rt(path)
    cp = rt.project.checkpoints.create(message, "manual")
    con.print(f"  ✓ {cp['id']}  {cp['commit_hash'][:7]}  {len(cp['files_changed'] or [])} files vs HEAD")


@app.command()
def checkpoints(path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """List checkpoints."""
    from .status import ago
    rt = _rt(path)
    cps = rt.project.checkpoints.list()
    _emit(cps, json_out, lambda: [con.print(Text.assemble((f"  {c['id']}  ", T.ACCENT), (f"{c['commit_hash'][:7]}  ", T.DIM), (f"{ago(c['ts']):9}", T.MUTED), (f"{c['kind']:12}", T.DIM), (c["task"][:60], T.TEXT))) for c in cps] or con.print("  none"))


@app.command()
def restore(cid: str = typer.Argument(..., autocompletion=_complete_checkpoint), path: str = PathOpt) -> None:
    """Restore a checkpoint by id."""
    rt = _rt(path)
    try:
        r = rt.project.checkpoints.restore(cid)
    except Exception as e:  # noqa: BLE001
        err.print(Text(f"✕ {e}", style=T.FAIL)); raise typer.Exit(1)
    con.print(f"  ✓ restored {cid}: {len(r['restored'])} restored, {len(r['deleted'])} removed.  Redo: genius restore {r['safety_checkpoint']}")


STATE_STYLE = {"CONNECTED": T.OK, "LIVE VERIFIED": T.OK, "ERROR": T.FAIL, "READY FOR KEY": T.MUTED, "NOT CONFIGURED": T.DIM, "DISABLED": T.DIM,
               "NEEDS MODEL": T.WARN, "KEY SET · UNTESTED": T.WARN, "KEY SET · NOT ADDED": T.WARN, "UNTESTED": T.MUTED}


async def _probe(rt: Runtime, names: list[str] | None = None, record: bool = True, local_only: bool = False):
    from .cli_probe import probe_providers
    return await probe_providers(rt.router, names, record, local_only)


@models_app.callback()
def _models(ctx: typer.Context, path: str = PathOpt, test_: bool = typer.Option(False, "--test", help="Health-check configured providers (never contacts a provider without a key)"), json_out: bool = JsonOpt) -> None:
    if ctx.invoked_subcommand:
        return
    from .providers.status import statuses
    rt = _rt(path)
    probes = _run(_probe(rt)) if test_ else _run(_probe(rt, record=False, local_only=True))     # default: only in-process/local providers are probed
    sts = statuses(rt.project.cfg, rt.gstore, probes)
    usage = {u["provider"]: u for u in rt.gstore.usage_by_provider()}
    rows = [{**s.to_dict(), "cost_today": round((usage.get(s.name) or {}).get("cost") or 0, 4), "errors": (usage.get(s.name) or {}).get("errors", 0)} for s in sts]
    def render():
        _head("Genius Dev · models")
        t = Table(box=None, pad_edge=False, show_edge=False, header_style=f"bold {T.DIM}", padding=(0, 2))
        for h in ("PROVIDER", "MODEL", "STATUS", "LATENCY", "KEY", "ROLE"):
            t.add_column(h, justify="right" if h == "LATENCY" else "left")
        for s in sts:
            role = "primary" if s.primary else "fallback" if s.fallback else ""
            t.add_row(Text(s.label, style=T.AI if s.configured else T.MUTED), s.model or Text("—", style=T.DIM), Text(s.state, style=STATE_STYLE.get(s.state, T.MUTED)),
                      f"{s.latency_ms}ms" if s.latency_ms is not None else "—", Text("YES" if s.key_present else "—" if s.local or s.kind == "mock" else "no", style=T.OK if s.key_present else T.DIM), Text(role, style=T.ACCENT))
        con.print(t)
        con.print()
        con.print(Text("  READY FOR KEY = implemented and mock-tested; add a key to go live:  genius models key <provider>  ·  genius models test <provider>", style=T.DIM))
    _emit(rows, json_out, render)


def _preset_or_die(name: str) -> dict:
    if name not in PRESETS:
        err.print(Text(f"✕ unknown provider '{name}'. Presets: {', '.join(PRESETS)} — or use `genius models add openai --name {name} --base-url …` for a custom OpenAI-compatible endpoint.", style=T.FAIL))
        raise typer.Exit(2)
    return PRESETS[name]


def _ensure_added(cfg: Config, name: str, preset: str | None = None, scope: str = "global") -> ProviderConfig:
    have = cfg.providers()
    if name in have:
        return have[name]
    p = ProviderConfig.from_dict(name, PRESETS[preset or name])
    cfg.save_provider(p, scope)
    return p


@models_app.command("add")
def models_add(preset: str = typer.Argument(..., help=" | ".join(PRESETS), autocompletion=_complete_preset), name: str = typer.Option(None, "--name"), model: str = typer.Option(None, "--model"),
               base_url: str = typer.Option(None, "--base-url"), key_env: str = typer.Option(None, "--key-env", help="Read the key from this environment variable instead of the Keychain"),
               input_cost: float = typer.Option(None, "--input-cost", help="USD per 1M input tokens"), output_cost: float = typer.Option(None, "--output-cost"),
               context: int = typer.Option(None, "--context", help="Context window (tokens)"), tier: int = typer.Option(None, "--tier", help="1 economy · 2 balanced · 3 strongest"),
               project: bool = typer.Option(False, "--project")) -> None:
    """Add a provider from a preset (or any OpenAI-compatible endpoint with --base-url). No key is needed to add it."""
    d = {**_preset_or_die(preset)}
    for k, v in (("model", model), ("base_url", base_url), ("input_cost", input_cost), ("output_cost", output_cost), ("tier", tier), ("context_window", context)):
        if v is not None:
            d[k] = v
    if key_env:
        d["api_key_ref"] = f"env:{key_env}"
    nm = name or preset
    Config(Path(".").resolve() if project else None).save_provider(ProviderConfig.from_dict(nm, d), "project" if project else "global")
    con.print(f"  ✓ added {nm}")
    if d["api_key_ref"] != "none":
        con.print(Text(f"  Next: genius models key {nm}   (stores the key in the macOS Keychain)", style=T.DIM))
    if not d.get("model"):
        con.print(Text(f"  Then: genius models select-model {nm} <model-id>", style=T.DIM))


@models_app.command("key")
def models_key(name: str = typer.Argument(..., autocompletion=_complete_provider), stdin: bool = typer.Option(False, "--stdin", help="Read the key from standard input (scripting)")) -> None:
    """Store a provider's API key in the macOS Keychain. The key is never echoed or printed."""
    cfg = Config(None)
    if name not in cfg.providers():
        _preset_or_die(name)
    p = _ensure_added(cfg, name)
    if p.api_key_ref == "none":
        con.print(Text(f"  {name} does not use an API key.", style=T.MUTED)); return
    key = sys.stdin.readline().strip() if stdin else typer.prompt(f"API key for {name} (input hidden)", hide_input=True)
    if not key:
        err.print(Text("✕ empty key — nothing stored", style=T.FAIL)); raise typer.Exit(1)
    if not keychain_set(name, key):
        err.print(Text("✕ macOS Keychain unavailable. Alternative: export the key in your shell and run  genius models add "
                       f"{name} --key-env <VAR_NAME>", style=T.FAIL)); raise typer.Exit(1)
    del key
    p.api_key_ref = f"keychain:{name}"
    cfg.save_provider(p)
    from .providers.status import clear_live
    clear_live(__import__("genius_dev.project", fromlist=["x"]).global_store(), name)
    con.print(f"  {p.name}\n  Configured: YES")
    con.print(Text(f"  Verify it:  genius models test {name}", style=T.DIM))


@models_app.command("remove-key")
def models_remove_key(name: str = typer.Argument(..., autocompletion=_complete_provider)) -> None:
    """Delete a stored key from the Keychain."""
    from .secrets import keychain_delete
    cfg = Config(None)
    ok = keychain_delete(name)
    if name in cfg.providers() and name in PRESETS:
        p = cfg.providers()[name]
        p.api_key_ref = PRESETS[name]["api_key_ref"]
        cfg.save_provider(p)
    from .providers.status import clear_live
    clear_live(__import__("genius_dev.project", fromlist=["x"]).global_store(), name)
    con.print(f"  {name}\n  Configured: NO" + ("" if ok else Text("  (no stored key was found)", style=T.DIM).plain))


@models_app.command("test")
def models_test(name: str = typer.Argument(None, autocompletion=_complete_provider), path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """Send one tiny request to verify a provider. A provider with no key is reported READY FOR KEY and is never contacted."""
    from .providers.status import statuses
    rt = _rt(path)
    cfgs = rt.router.configs()
    if name and name not in cfgs and name in PRESETS:
        pre = statuses(rt.project.cfg, rt.gstore)
        s = next(x for x in pre if x.name == name)
        _emit(s.to_dict(), json_out, lambda: con.print(Text(f"  {s.label}\n  {s.state}" + (f" — {s.detail}" if s.detail else ""), style=STATE_STYLE.get(s.state, T.MUTED))))
        raise typer.Exit(0 if s.state in ("READY FOR KEY", "NOT CONFIGURED") else 2)
    probes = _run(_probe(rt, [name] if name else None))
    sts = [s for s in statuses(rt.project.cfg, rt.gstore, probes) if (not name or s.name == name) and (s.configured or name)]
    def render():
        for s in sts:
            con.print(Text.assemble((f"  {s.label:12}", T.TEXT), (s.state, STATE_STYLE.get(s.state, T.MUTED)), (f"  {s.latency_ms}ms" if s.latency_ms is not None else "", T.MUTED), (f"  {s.detail}" if s.detail else "", T.DIM)))
    _emit([s.to_dict() for s in sts], json_out, render)
    bad = [s for s in sts if s.state == "ERROR"]
    raise typer.Exit(1 if bad else 0)


@models_app.command("list-models")
def models_list(name: str = typer.Argument(..., autocompletion=_complete_provider), path: str = PathOpt) -> None:
    """List model IDs the provider offers (needs a key for cloud providers)."""
    rt = _rt(path)
    if name not in rt.router.configs():
        err.print(Text(f"✕ {name} is not added. Run: genius models add {name}", style=T.FAIL)); raise typer.Exit(1)
    from .secrets import has_key
    c = rt.router.configs()[name]
    if c.api_key_ref != "none" and not has_key(c.api_key_ref):
        con.print(Text(f"  {name}: READY FOR KEY — add a key first: genius models key {name}", style=T.MUTED)); raise typer.Exit(2)
    prov = rt.router.provider(name)
    ids = _run(prov.list_models()) if hasattr(prov, "list_models") else []
    for i in ids[:80]:
        con.print("  " + i)
    if not ids:
        con.print(Text("  (no model list available — set one manually: genius models select-model …)", style=T.DIM))


@models_app.command("select-model")
def models_select(name: str = typer.Argument(..., autocompletion=_complete_provider), model_id: str = typer.Argument(...), project: bool = typer.Option(False, "--project")) -> None:
    """Choose the model ID a provider uses."""
    cfg = Config(Path(".").resolve() if project else None)
    p = _ensure_added(cfg, name)
    p.model = model_id
    cfg.save_provider(p, "project" if project else "global")
    con.print(f"  ✓ {name} → {model_id}")


@models_app.command("configure")
def models_configure(name: str = typer.Argument(..., autocompletion=_complete_provider), base_url: str = typer.Option(None, "--base-url"), context: int = typer.Option(None, "--context"),
                     input_cost: float = typer.Option(None, "--input-cost"), output_cost: float = typer.Option(None, "--output-cost"), tier: int = typer.Option(None, "--tier"),
                     enable: Optional[bool] = typer.Option(None, "--enable/--disable"), key_env: str = typer.Option(None, "--key-env")) -> None:
    """Edit a provider's endpoint, limits, pricing, strength tier or enabled state."""
    cfg = Config(None)
    p = _ensure_added(cfg, name)
    for k, v in (("base_url", base_url), ("context_window", context), ("input_cost", input_cost), ("output_cost", output_cost), ("tier", tier), ("enabled", enable)):
        if v is not None:
            setattr(p, k, v)
    if key_env:
        p.api_key_ref = f"env:{key_env}"
    cfg.save_provider(p)
    con.print(f"  ✓ {name} updated")


@models_app.command("set-primary")
def models_primary(name: str = typer.Argument(..., autocompletion=_complete_provider), project: bool = typer.Option(False, "--project")) -> None:
    """Use this provider for every role (unless a role is pinned or routed by hand)."""
    cfg = Config(Path(".").resolve() if project else None)
    _ensure_added(cfg, name)
    cfg.set("general.primary", name, "project" if project else "global")
    con.print(f"  ✓ primary → {name}")


@models_app.command("set-fallback")
def models_fallback(names: list[str] = typer.Argument(None, autocompletion=_complete_provider), clear: bool = typer.Option(False, "--clear")) -> None:
    """Fallback order tried when the primary fails: genius models set-fallback claude openai"""
    cfg = Config(None)
    chain = [] if clear else list(names or [])
    for n in chain:
        _ensure_added(cfg, n)
    cfg.set("fallback.chain", chain)
    con.print("  ✓ fallback → " + (" → ".join(chain) or "none"))


@models_app.command("remove")
def models_remove(name: str = typer.Argument(..., autocompletion=_complete_provider)) -> None:
    """Remove a provider entry (and its Keychain key)."""
    from .secrets import keychain_delete
    cfg = Config(None)
    cfg._global.get("providers", {}).pop(name, None)
    cfg._save("global")
    keychain_delete(name)
    con.print(f"  ✓ removed {name}")


@app.command()
def model(role: str = typer.Argument(None, help="Role to explain (planner, implementer, debugger, reviewer, ui_reviewer…). Omit for all roles."),
          use: str = typer.Option(None, "--use", help="Pin this role to a provider for this project (persisted)", autocompletion=_complete_provider),
          mode: str = typer.Option(None, "--mode", help="economy|balanced|max_quality|local_only|custom"), path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """Show (or set) which model handles each role, and why."""
    rt = _rt(path)
    if mode:
        rt.project.cfg.set("general.routing_mode", mode, "project")
        rt = _rt(path)
    if use:
        rt.project.cfg.set(f"routing.custom.{role or 'implementer'}", use, "project")
        rt = _rt(path)
    rt.router.mode_override = None
    roles = [role] if role else ["planner", "architect", "implementer", "debugger", "tester", "reviewer", "security_reviewer", "final_reviewer", "ui_reviewer", "summarizer"]
    rows = []
    for r_ in roles:
        rr = rt.router.route(r_)
        rows.append({**rr.explain(), "role": r_, "mode": rt.router.mode, "reason": rr.reason})
    def render():
        _head(f"routing · mode {rt.router.mode}")
        for x in rows:
            con.print(Text.assemble((f"  {x['role']:18}", T.DIM), (f"{(x['selected'] or '—'):14}", T.AI if x["selected"] else T.WARN), (f"{('→ ' + ', '.join(x['fallback'])) if x['fallback'] else '':18}", T.DIM), (x["reason"], T.MUTED)))
    _emit(rows[0] if role else rows, json_out, render)


@app.command()
def budget(amount: float = typer.Argument(None, help="Max estimated API spend per day in USD (0 = unlimited)"), path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """Show or set the daily API budget."""
    rt = _rt(path)
    if amount is not None:
        rt.project.cfg.set("general.daily_budget", amount)
    spent, limit = rt.router.budget()
    d = {"spent_today": round(spent, 4), "limit": limit, "state": rt.router.budget_state()}
    _emit(d, json_out, lambda: con.print(f"  {T.money(spent)} of {T.money(limit) if limit else 'unlimited'} today" + (f"  ({d['state']})" if limit else "")))


@app.command()
def costs(path: str = PathOpt, json_out: bool = JsonOpt, all_time: bool = typer.Option(False, "--all"),
          by: str = typer.Option("provider", "--by", help="provider | role | model | project")) -> None:
    """Token usage and estimated cost (by provider, agent role, model or project)."""
    rt = _rt(path)
    if by != "provider":
        g = rt.gstore.usage_grouped(by, 0 if all_time else None)
        def render_g():
            _head(f"by {by} · " + ("all time" if all_time else "today"))
            for r in g:
                con.print(Text.assemble((f"  {str(r['key']):18}", T.AI), (f"{T.money(r['cost'] or 0):>9}", T.TEXT), (f"   {r['requests']} req · {r['input_tokens'] or 0:,} in · {r['output_tokens'] or 0:,} out", T.MUTED)))
            if not g:
                con.print("  no calls recorded")
        _emit({"by": by, "rows": g, "total": round(sum(r["cost"] or 0 for r in g), 4)}, json_out, render_g)
        return
    rows = rt.gstore.usage_by_provider(0 if all_time else None)
    total = sum(r["cost"] or 0 for r in rows)
    def render():
        _head("all time" if all_time else "today")
        for r in rows:
            con.print(Text.assemble((f"  {r['provider']:14}", T.AI), (f"{T.money(r['cost'] or 0):>9}", T.TEXT), (f"   {r['requests']} req · {r['input_tokens']:,} in · {r['output_tokens']:,} out · {r['cached_tokens']:,} cached · {r['latency'] or 0:.1f}s avg", T.MUTED)))
        con.print(Text.assemble((f"\n  {'TOTAL':14}", T.DIM), (f"{T.money(total):>9}", f"bold {T.TEXT}")))
        if any((r["cost"] or 0) == 0 and r["requests"] for r in rows):
            con.print(Text("  Providers with $0 have no pricing configured (input_cost/output_cost per 1M tokens).", style=T.DIM))
    _emit({"providers": rows, "total": round(total, 4)}, json_out, render)


@app.command()
def tasks(path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """Task tree."""
    rt = _rt(path)
    flat = rt.project.tasks.flat()
    def render():
        for depth, t in flat:
            con.print(Text.assemble(("  " + "    " * depth, ""), T.status_mark(t["status"]), (f" {t['title']}", T.TEXT if t["status"] != "done" else T.MUTED), (f"  {' '.join(t['req_ids'])}", T.DIM)))
        if not flat:
            con.print("  no tasks yet")
    _emit([t for _, t in flat], json_out, render)


@app.command()
def requirements(path: str = PathOpt, json_out: bool = JsonOpt, waive: str = typer.Option(None, "--waive", help="Waive a requirement (e.g. REQ-004)"),
                 add: str = typer.Option(None, "--add", help="Add a requirement"),
                 import_: str = typer.Option(None, "--import", help="Import requirements from a JSON file: [{description, verify}]"),
                 verify: bool = typer.Option(False, "--verify", help="Run every verification now and set statuses from the evidence")) -> None:
    """Requirements with verification status."""
    rt = _rt(path, permission="standard")
    if import_:
        have = {r["description"] for r in rt.project.reqs.all()}
        for item in json.loads(Path(import_).read_text()):
            if item["description"] not in have:
                rt.project.reqs.add(item["description"], item.get("source", "import"), item.get("verify", "manual"), item.get("files", []))
        rt.project.sync_memory()
    if verify:
        verdicts = _run(rt.agent.verify_only())
        con.print(Text(f"  verified {len(verdicts)} requirement(s) from fresh evidence", style=T.DIM))
    if waive:
        rt.project.reqs.waive(waive.upper(), "waived by user"); rt.project.sync_memory()
    if add:
        rid = rt.project.reqs.add(add, "user", "manual"); rt.project.sync_memory(); con.print(f"  added {rid}")
    reqs = rt.project.reqs.all()
    sm = rt.project.reqs.summary()
    def render():
        c = sm.counts
        con.print(Text(f"  {c['PASS']} PASS   {c['FAIL']} FAIL   {c['BLOCKED']} BLOCKED   {c['WAIVED']} WAIVED   {c['NOT_STARTED'] + c['IN_PROGRESS']} OPEN   ·  {sm.resolved_pct:.1f}% resolved", style=T.MUTED))
        con.print()
        for r in reqs:
            con.print(Text.assemble(("  ", ""), T.status_mark(r["status"]), (f" {r['id']}  ", T.DIM), (r["description"][:70], T.TEXT), (f"   {r['result'][:40]}" if r["result"] else "", T.DIM)))
    _emit({"summary": {**sm.counts, "resolved_pct": sm.resolved_pct}, "requirements": reqs}, json_out, render)


@app.command()
def handoff(path: str = PathOpt) -> None:
    """Write a compact handoff package for another agent or model."""
    from .handoff import build_handoff
    rt = _rt(path)
    spent, _ = rt.router.budget()
    _, out = build_handoff(rt.project, f"{T.money(spent)} today")
    con.print(f"  ✓ {out}")


@app.command()
def review(path: str = PathOpt, ui: bool = typer.Option(False, "--ui", help="Launch the app and review screens"), json_out: bool = JsonOpt,
           repair: bool = typer.Option(False, "--repair", help="With --ui: let the agent fix failures")) -> None:
    """Review recent changes (or, with --ui, the running app)."""
    rt = _rt(path, ask=_cli_ask)
    if ui:
        from .tools import ToolBox
        from .visual import visual_qa
        rep = _run(visual_qa(rt.project, ToolBox(rt.project, rt.perms, rt.bus), rt.router, rt.bus))
        _emit({"ran": rep.ran, "failures": rep.failures, "warnings": rep.warnings, "console": rep.console_errors, "reason": rep.reason}, json_out, lambda: con.print(rep.text()))
        if repair and rep.ran and not rep.ok:
            rt.bus.subscribe(_print_event)
            _run(rt.agent.run("Fix these UI problems:\n" + "\n".join(rep.failures[:10] + rep.console_errors[:5]), ui_checks=False))
        raise typer.Exit(0 if rep.ok else 1)
    from .audit import review_diff
    d = _run(review_diff(rt.project, rt.router))
    def render():
        _head(f"review · {d.get('verdict', '?')}")
        con.print("  " + d.get("summary", ""))
        for f in d.get("findings", []):
            con.print(Text(f"  [{f.get('severity')}] {f.get('file')}: {f.get('issue')}", style=T.WARN))
    _emit(d, json_out, render)


@app.command()
def debate(question: str = typer.Argument(...), path: str = PathOpt, models: int = typer.Option(3, "--models", "-m"), json_out: bool = JsonOpt) -> None:
    """Independent hypotheses from several models, judged against real evidence."""
    from .debate import run_debate
    rt = _rt(path)
    r = _run(run_debate(rt.project, rt.router, rt.perms, rt.bus, question, models))
    _emit(r.to_dict(), json_out, lambda: con.print(r.text()))


@app.command()
def debug(path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """Gather failures, logs and recent changes; form and test hypotheses."""
    from .debate import run_debate
    rt = _rt(path)
    ev = [f"{e['label']}: {e['message']}" for e in (dict(r) for r in rt.project.store.query("SELECT label,message FROM events WHERE category='ERRORS' ORDER BY id DESC LIMIT 8"))]
    logs_ = "\n".join(f"[{p['name']}] " + rt.project.procs.tail(p["name"], 8) for p in rt.project.procs.list())
    issue = "Diagnose the project's current failures.\nRecent errors:\n" + ("\n".join(ev) or "none") + ("\nProcess logs:\n" + logs_ if logs_.strip() else "")
    r = _run(run_debate(rt.project, rt.router, rt.perms, rt.bus, issue, 1))
    _emit(r.to_dict(), json_out, lambda: con.print(r.text()))


@app.command()
def security(path: str = PathOpt, json_out: bool = JsonOpt, deps: bool = typer.Option(False, "--deps", help="Also run npm audit / pip-audit (needs network, no key)")) -> None:
    """Static security scan: secrets, risky APIs, tracked secret files (and, with --deps, known-vulnerable dependencies)."""
    from .audit import security_findings
    rt = _rt(path)
    rt.project.index.refresh()
    fs = security_findings(rt.project)
    if deps:
        from .deps import dependency_findings
        dfs, notes = _run(dependency_findings(rt.project))
        fs += dfs
        for n in notes:
            con.print(Text("  " + n, style=T.DIM))
    def render():
        _head(f"security · {len(fs)} findings")
        for f in sorted(fs, key=lambda f: {"high": 0, "medium": 1, "low": 2}[f.severity]):
            con.print(Text(f"  {f.text()}", style={"high": T.FAIL, "medium": T.WARN, "low": T.MUTED}[f.severity]))
        if not fs:
            con.print(Text("  ✓ no findings", style=T.OK))
    _emit([f.__dict__ for f in fs], json_out, render)
    raise typer.Exit(1 if any(f.severity == "high" for f in fs) else 0)


@app.command()
def context(goal: str = typer.Argument("", help="Goal to build context for"), path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """Show what would be sent to a model for a goal (files, size, redactions)."""
    from .context import ContextBuilder
    rt = _rt(path)
    rt.project.index.refresh()
    goal = goal or (rt.project.tasks.current() or {}).get("title") or (rt.project.last_session() or {}).get("goal") or ""
    pack = ContextBuilder(rt.project).build(goal)
    prim = rt.router.route("implementer").primary
    win = rt.router.configs()[prim].context_window if prim else 0
    d = {"goal": goal, "tokens": pack.tokens, "window": win, "pct": round(100 * pack.tokens / win, 1) if win else None, "files": pack.files, "withheld_secret_files": pack.skipped_secret_files}
    def render():
        con.print(f"  goal     {goal or '—'}\n  size     ~{pack.tokens:,} tokens" + (f" ({d['pct']}% of {win:,})" if win else ""))
        for f in pack.files:
            con.print(f"    · {f}")
        for f in pack.skipped_secret_files:
            con.print(Text(f"    ⊘ {f} (secret file withheld)", style=T.WARN))
    _emit(d, json_out, render)


@app.command()
def search(query: str = typer.Argument(None, help="Natural-language or keyword query"), path: str = PathOpt, json_out: bool = JsonOpt, n: int = typer.Option(15, "-n"),
           symbol: str = typer.Option(None, "--symbol", "-s", help="Symbol name (fuzzy): functions, classes, components, routes, tables"),
           file: str = typer.Option(None, "--file", "-f", help="File name (fuzzy)"), text: str = typer.Option(None, "--text", "-t", help="Exact text / regex search in file contents"),
           semantic_only: bool = typer.Option(False, "--semantic", help="Semantic (concept-aware) search only"), regex: bool = typer.Option(False, "--regex")) -> None:
    """Search the project: text, symbols, files and semantic (offline, no API)."""
    from .semantic import semantic_extra, semantic_search
    rt = _rt(path)
    ix = rt.project.index
    ix.refresh()
    hits: list[dict] = []
    add = lambda kind, title, p_, line, score, snippet="": hits.append({"kind": kind, "title": title, "path": p_, "line": line, "score": round(score, 2), "snippet": snippet})
    if symbol:
        for h in ix.search(symbol, n, ("symbol", "route", "table")):
            add(h.kind, h.text, h.path, h.line, h.score)
    elif file:
        for h in ix.search(file, n, ("file",)):
            add("file", h.path, h.path, 0, h.score)
    elif text:
        for h in ix.grep(text, n, regex=regex):
            add("text", h.text[:80], h.path, h.line, 1.0)
    else:
        if not query:
            err.print("give a query, or use --symbol / --file / --text"); raise typer.Exit(2)
        for sh in semantic_search(ix, query, n):
            add("semantic:" + sh["kind"], sh["title"], sh["path"], sh["line"], sh["score"], sh["snippet"])
        for xh in semantic_extra(query, rt.project.reqs.all(), [t for _, t in rt.project.tasks.flat()], 5):
            add(xh["kind"], f"{xh['title']}  {xh['snippet']}", "", 0, xh["score"])
        if not semantic_only:
            seen = {(h["path"], h["line"]) for h in hits}
            for h in ix.search(query, n, ("symbol", "route", "table", "file")):
                if (h.path, h.line) not in seen and h.score > 45:
                    add(h.kind, h.text, h.path, h.line, h.score / 20); seen.add((h.path, h.line))
            for h in ix.grep(query, 5):
                if (h.path, h.line) not in seen:
                    add("text", h.text[:80], h.path, h.line, 0.5)
    def render():
        if not hits:
            con.print(Text("  no matches", style=T.DIM)); return
        for h in hits[:n]:
            con.print(Text.assemble((f"  {h['kind'][:16]:17}", T.DIM), (f"{fit_(h['title'], 44):46}", T.TEXT), (f"{h['path']}:{h['line']}" if h["path"] else "", T.MUTED)))
            if h["snippet"] and h["kind"].startswith("semantic"):
                con.print(Text("    " + h["snippet"], style=T.DIM))
    _emit(hits[:n], json_out, render)


def fit_(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


@app.command()
def clean(path: str = PathOpt, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    """Remove Genius Dev artefacts (screenshots, stale logs, old handoffs). Never touches your code."""
    import time
    rt = _rt(path)
    g = rt.project.gdir
    removed = []
    for d in ("screenshots",):
        for f in (g / d).glob("*"):
            removed.append(f); None if dry_run else f.unlink()
    live = {p["name"] for p in rt.project.procs.list() if p["status"] == "running"}
    for f in (g / "logs").glob("*.log"):
        if f.stem not in live:
            removed.append(f); None if dry_run else f.unlink()
    hs = sorted((g / "handoffs").glob("*.md"))[:-5]
    for f in hs:
        removed.append(f); None if dry_run else f.unlink()
    if not dry_run:
        rt.project.store.execute("DELETE FROM processes WHERE status!='running'")
        rt.project.store.execute("DELETE FROM events WHERE ts<?", (time.time() - 14 * 86400,))
        rt.project.store.execute("VACUUM")
    con.print(f"  {'would remove' if dry_run else 'removed'} {len(removed)} file(s)")


@app.command()
def protect(paths: list[str] = typer.Argument(..., help="Files/dirs the agent may not modify"), path: str = PathOpt, remove: bool = typer.Option(False, "--remove")) -> None:
    """Protect files or directories from agent modification."""
    rt = _rt(path)
    cur = list(rt.project.cfg.get("protect.paths", []))
    for p in paths:
        p = p.strip("/")
        if remove and p in cur:
            cur.remove(p)
        elif not remove and p not in cur:
            cur.append(p)
    rt.project.cfg.set("protect.paths", cur, "project")
    con.print("  protected: " + (", ".join(cur) or "none"))


@app.command()
def config(key: str = typer.Argument(None, help="dotted key, e.g. general.permission"), value: str = typer.Argument(None), project: bool = typer.Option(False, "--project"),
           show_path: bool = typer.Option(False, "--path"), path: str = PathOpt) -> None:
    """Show or edit configuration (global: ~/.genius-dev/config.toml, project: .genius/config.toml)."""
    root = Path(path).resolve()
    cfg = Config(root if (root / ".genius").exists() else None)
    if show_path:
        con.print(f"global:  {cfg.global_path}\nproject: {cfg.project_path or '—'}")
        return
    if key and value is not None:
        v: Any = value
        if value.lower() in ("true", "false"):
            v = value.lower() == "true"
        else:
            try:
                v = float(value) if "." in value else int(value)
            except ValueError:
                pass
        cfg.set(key, v, "project" if project else "global")
        con.print(f"  ✓ {key} = {v}")
        return
    if key:
        con.print(json.dumps(cfg.get(key), indent=2, default=str))
        return
    con.print(redact(dumps_toml(cfg.data)))


DEMOS = {"fix": ("demo_project", "Fix the failing tests in the calculator", "two independent bugs, one repair pass"),
         "debug": ("demo_debug", "Fix slugify so all of its tests pass", "a too-shallow first fix fails verification; the debugger diagnoses and repairs"),
         "web": ("demo_web", "Build a landing page for Acme with a Get started button", "real browser audit catches layout/a11y failures; the debugger fixes them")}
PHASES = {"INSPECT": "Inspect the project", "PLAN": "Requirements & plan", "CHECKPOINT": "Create a checkpoint", "READ": "Inspect files", "FIND": "Inspect files",
          "EDIT": "Modify code", "WRITE": "Modify code", "RETRY": "Diagnose and repair", "VERIFY": "Verify requirements", "BROWSER": "Visual QA (real browser)", "AUDIT": "Visual QA (real browser)"}


@app.command()
def demo(scenario: str = typer.Option("fix", "--scenario", "-s", help="fix | debug | web"), keep: bool = typer.Option(False, "--keep", help="Keep the demo project directory afterwards"),
         fast: bool = typer.Option(False, "--fast")) -> None:
    """Offline demo: a deterministic scripted model does real work on a broken project. No API key, no network."""
    import shutil, subprocess, tempfile, time
    import genius_dev
    if scenario not in DEMOS:
        err.print(f"unknown scenario '{scenario}' — choose: {', '.join(DEMOS)}"); raise typer.Exit(2)
    folder, goal, blurb = DEMOS[scenario]
    src = Path(genius_dev.__file__).parent / folder
    tmp = Path(tempfile.mkdtemp(prefix="genius-demo-")) / f"{scenario}-demo"
    shutil.copytree(src, tmp)
    subprocess.run(["git", "init", "-q"], cwd=tmp)
    rt = build_runtime(tmp, permission="autonomous")
    rt.project.cfg.save_provider(ProviderConfig.from_dict("mock", {**PRESETS["mock"], "input_cost": 0.5, "output_cost": 2.0}), "project")   # illustrative prices so cost tracking is visible
    rt.router.refresh()
    con.print(Text("GENIUS DEV · DEMO", style=f"bold {T.TEXT}"), Text(f"  {scenario}: {blurb}", style=T.MUTED))
    con.print(Text("  deterministic scripted model · real files, real tests, real subprocesses · no network\n", style=T.DIM))
    con.print(Text.assemble(("  GOAL  ", T.DIM), (goal + "\n", T.TEXT)))
    from .tools import ToolBox
    if scenario != "web":
        before = _run(ToolBox(rt.project, rt.perms, rt.bus).execute("tests", {}))
        con.print(Text(f"  Before: {before.summary} — {before.data.get('failed', 0)} failing\n", style=T.FAIL))
    phase = {"cur": ""}
    def show(ev):
        label = PHASES.get(ev.label, "")
        if ev.label in ("TEST", "FAIL") and ev.category in ("TESTS", "ERRORS") and "tests" in ev.message.lower():
            label = "Run tests" if ev.category == "TESTS" and "PASS" in ev.label + ev.message or "passed" in ev.message else "Tests fail — diagnose"
        if ev.label == "TEST" and "/" in ev.message:
            label = "Run tests"
        if label and label != phase["cur"]:
            phase["cur"] = label
            con.print(Text(f"\n  ▸ {label}", style=f"bold {T.ACCENT}"))
        _print_event(ev)
        if not fast:
            time.sleep(0.10)
    rt.bus.subscribe(show)
    rep = _run(rt.agent.run(goal))
    con.print(Text("\n  ▸ Update progress & finish", style=f"bold {T.ACCENT}"))
    con.print("\n" + rep.text())
    rows = rt.gstore.usage_by_provider()
    cost = sum(r["cost"] or 0 for r in rows)
    calls = sum(r["requests"] for r in rows)
    con.print(Text(f"\n  Model cost tracked: {T.money(cost)} across {calls} calls (mock pricing is illustrative)   ·   memory: {tmp / '.genius'}" if keep else f"\n  Model cost tracked: {T.money(cost)} across {calls} calls (mock pricing is illustrative)", style=T.DIM))
    con.print(Text(f"  Project kept at {tmp}" if keep else "", style=T.DIM))
    if not keep:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    raise typer.Exit(0 if rep.success else 1)


@app.command()
def plugins(path: str = PathOpt, json_out: bool = JsonOpt) -> None:
    """List loaded plugins, what they contribute, and any load errors."""
    from .plugins import load_registry
    reg = load_registry(Path(path).resolve())
    rows = [{"name": p.name, "source": reg.sources[p.name], "contributes": p.contributes(), "description": p.description, "tools": [t.name for t in p.tools()]} for p in reg.plugins]
    def render():
        for r in rows:
            con.print(Text.assemble((f"  {r['name']:12}", T.AI), (f"{r['source']:16}", T.DIM), (", ".join(r["contributes"]), T.MUTED)))
            con.print(Text("    " + r["description"] + (f"   tools: {', '.join(r['tools'])}" if r["tools"] else ""), style=T.DIM))
        for e in reg.errors:
            con.print(Text("  ! " + e, style=T.WARN))
    _emit({"plugins": rows, "errors": reg.errors}, json_out, render)


@app.command("browser-install")
def browser_install() -> None:
    """Download Chromium for browser testing / visual QA (works from the standalone binary too)."""
    import subprocess
    from .browser import configure_browser_path, playwright_installed
    configure_browser_path()
    if not playwright_installed():
        err.print(Text("✕ Playwright is not installed: pip install 'genius-dev[browser]'", style=T.FAIL)); raise typer.Exit(1)
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    node, cli_js = compute_driver_executable()
    con.print(Text("  Downloading Chromium (~150 MB) …", style=T.DIM))
    raise typer.Exit(subprocess.run([str(node), str(cli_js), "install", "chromium"], env=get_driver_env()).returncode)


@app.command("help")
def help_(ctx: typer.Context) -> None:
    """Show help."""
    from .controller import HELP
    con.print(ctx.parent.get_help() if ctx.parent else "")
    con.print("\n" + HELP)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
