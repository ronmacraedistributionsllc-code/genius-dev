"""Pure render functions: (app, width, height) -> Rich renderable. No widgets, no layout boxes — spacing and rules only."""
from __future__ import annotations

import re
import time
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text

from .. import theme as T
from ..status import ago
from .widgets import fit, hint, render_diff, section

CAT_STYLE = {"ERRORS": T.FAIL, "TESTS": T.ACCENT, "BUILD": T.ACCENT, "BROWSER": T.ACCENT, "MODEL": T.AI, "TOOLS": T.MUTED, "AGENT": T.TEXT, "SYSTEM": T.MUTED}
VIEW_TITLES = {"home": "Home", "tasks": "Tasks", "requirements": "Requirements", "models": "Models", "tools": "Tools", "tests": "Tests", "preview": "Preview",
               "diff": "Diff", "doctor": "Doctor", "logs": "Logs", "checkpoints": "Checkpoints", "cost": "Cost", "project": "Project", "settings": "Settings", "output": "Result"}


def grid2(widths: tuple = (None,), right: tuple = (), gap: int = 2) -> Table:
    t = Table.grid(padding=(0, gap), expand=True)
    for i, w in enumerate(widths):
        t.add_column(width=w, ratio=1 if w is None else None, no_wrap=True, overflow="ellipsis", justify="right" if i in right else "left")
    return t


def _kv(label: str, value: Any, style: str = T.TEXT) -> tuple[Text, Text]:
    return Text(label.upper(), style=T.DIM), value if isinstance(value, Text) else Text(str(value), style=style)


def _event_row(ev, now: float, w: int) -> tuple[Text, Text, Text]:
    fresh = now - ev.ts < 2.0
    color = CAT_STYLE.get(ev.category, T.MUTED)
    if ev.label in ("VERIFY", "DONE") and "PASS" in ev.message:
        color = T.OK
    msg_style = T.TEXT if fresh or ev.category in ("ERRORS",) else T.MUTED
    return Text(ev.clock, style=T.DIM), Text(ev.label, style=color), Text(fit(ev.message, max(20, w - 22)), style=msg_style)


# ------------------------------------------------------------------------------------------------ HOME
def home(app, w: int, h: int) -> Group | Table:
    s = app.snap
    now = time.time()
    a = s["agent"]
    running = a["status"] in ("running", "paused")
    wide = w >= 96
    main_w = w - 38 if wide else w
    parts: list[Any] = []

    # objective
    parts.append(section("Objective"))
    goal = a["task"] if running else (s["last_session"]["goal"] if s["last_session"] else "")
    if goal:
        parts.append(Text(fit(goal.replace("\n", " "), main_w), style=f"bold {T.TEXT}"))
    else:
        parts.append(Text("No objective yet", style=T.MUTED))
    if running:
        spin = T.SPINNER[int(now * 10) % len(T.SPINNER)] if a["status"] == "running" else "⏸"
        el = int(now - a["started"])
        parts.append(Text.assemble((f"{spin} ", T.ACCENT), (fit(a["op"] or "Working", main_w - 22), T.TEXT), (f"   step {a['step']} · {el // 60}:{el % 60:02d}", T.DIM)))
    else:
        ls = s["last_session"]
        parts.append(Text(f"Idle · last run {ago(ls['ended'] or ls['started'])} — {ls['status']}" if ls else "Idle", style=T.MUTED))
    parts.append(Text())

    # progress
    pct = s["progress"]
    parts.append(Text.assemble(section("Progress"), ("  " + f"{pct:.0f}%", f"bold {T.TEXT}")))
    parts.append(T.bar(pct, max(10, min(60, main_w - 2))))
    if not wide:
        parts += [Text(), _metrics(s, w, False)]
    parts.append(Text())

    # tasks
    flat = app.rt.project.tasks.flat()
    budget_rows = max(0, h - 17)
    if flat and budget_rows >= 3:
        n = max(3, min(9, budget_rows // 2))
        active_idx = next((i for i, (_, t) in enumerate(flat) if t["status"] == "active"), None)
        start = 0 if active_idx is None or active_idx < n - 2 else active_idx - (n - 3)
        parts.append(section("Tasks", f"{sum(1 for _, t in flat if t['status'] == 'done')}/{len(flat)}"))
        for depth, t in flat[start: start + n]:
            parts.append(Text.assemble(("  " * depth, ""), T.status_mark(t["status"]), (" " + fit(t["title"], main_w - 4 - 2 * depth), T.TEXT if t["status"] in ("active", "todo") else T.MUTED)))
        parts.append(Text())

    # activity
    act_n = max(3, min(14, h - 13 - (min(9, max(3, budget_rows // 2)) + 2 if flat and budget_rows >= 3 else 0)))
    evs = [e for e in app.rt.bus.buffer if e.label not in ("ROUTE", "YOU") and (e.category != "TOOLS" or e.label in ("EDIT", "WRITE", "READ", "RUN", "TEST", "FIND", "BROWSER", "GIT"))][-act_n:]
    parts.append(section("Activity"))
    if evs:
        t = grid2((8, 9, None))
        for e in evs:
            t.add_row(*_event_row(e, now, main_w))
        parts.append(t)
    elif not goal:
        parts += [Text("Describe what you want — for example:", style=T.MUTED), Text(), Text("  Build me a delivery management app", style=T.TEXT),
                  Text("  Fix signup and make sure login actually works", style=T.TEXT), Text("  Find everything preventing this app from being production ready", style=T.TEXT)]
        cf = app.rt.router.configs()
        if s["model"] and s["model"] in cf and cf[s["model"]].kind == "mock":
            parts += [Text(), Text("Running on the offline demo model. Add a provider any time:  ^O models → a add key", style=T.DIM)]
    else:
        parts.append(Text("Nothing yet", style=T.DIM))
    rep = app.ctl.last_report
    if not running and rep is not None:
        ok = rep.success
        parts += [Text(), Text.assemble(section("Last result"), ("   ", ""), (rep.status, T.OK if ok else T.WARN), (f"   {rep.summary}"[:main_w], T.MUTED))]
        lines = [("✓", T.OK, v) for v in rep.verified[:3]] if ok else [("!", T.WARN, v) for v in (rep.limitations + rep.blockers)[:4]]
        for glyph, col, v in lines:
            parts.append(Text.assemble((f"{glyph} ", col), (fit(v, main_w - 2), T.MUTED)))
        extra = f"{rep.test_results}" + (f" · {len(rep.files_changed)} file(s) changed" if rep.files_changed else "")
        parts.append(Text(fit(extra + "   ·   full report: ctrl+k → Go to Result", main_w), style=T.DIM))
    left = Group(*parts)
    metrics = _metrics(s, 30, True)
    if not wide:
        return left
    t = Table.grid(padding=(0, 6), expand=True)
    t.add_column(ratio=1)
    t.add_column(width=30)
    t.add_row(left, metrics)
    return t


def _metrics(s: dict, width: int, wide: bool) -> Group | Text:
    tst, rq = s["tests"], s["requirements"]
    if tst:
        tests = Text.assemble((f"{tst['passed']} pass", T.OK if tst["ok"] else T.TEXT), (f" · {tst['failed']} fail", T.FAIL) if tst["failed"] else ("", ""))
    else:
        tests = Text("—", style=T.DIM)
    build = Text("—", style=T.DIM) if s["build"] is None else Text("PASS", style=T.OK) if s["build"] else Text("FAIL", style=T.FAIL)
    ctx = s["context_pct"]
    cost = Text.assemble((T.money(s["cost_today"]), T.WARN if s["budget_state"] != "ok" else T.TEXT), (f" / {T.money(s['budget'])}", T.DIM) if s["budget"] else ("", ""))
    rows = [
        ("Tests", tests), ("Build", build),
        ("Reqs", Text(f"{rq['PASS']} / {rq['total']}" if rq["total"] else "—", style=T.TEXT if rq["total"] else T.DIM)),
        ("Context", Text(f"{ctx}%", style=T.WARN if ctx >= 70 else T.TEXT)),
        ("Cost today", cost), (" ", Text("")),
        ("Model", Text(s["model"] or "none", style=T.AI if s["model"] else T.WARN)),
        ("Fallback", Text(s["fallback"] or "—", style=T.AI if s["fallback"] else T.DIM)),
        ("Routing", Text(s["routing_mode"].replace("_", " "), style=T.MUTED)),
        ("Permission", Text(s["permission"], style=T.MUTED)),
    ]
    if not wide:
        line = Text()
        for i, (k, v) in enumerate([r for r in rows if r[0] not in (" ", "Permission", "Routing", "Fallback")]):
            line.append(("" if i == 0 else "   ") + ("COST" if k == "Cost today" else k.upper()) + " ", style=T.DIM)
            line.append_text(v)
        return line
    t = grid2((12, None), right=(1,))
    for k, v in rows:
        t.add_row(Text(k.upper(), style=T.DIM), v)
    ret: list[Any] = [section("Status"), t]
    return Group(*ret)


# ------------------------------------------------------------------------------------------------ OTHER VIEWS
def tasks_view(app, w: int, h: int) -> Group:
    flat = app.rt.project.tasks.flat()
    out: list[Any] = [section("Tasks", f"{app.rt.project.tasks.progress():.0f}% complete"), Text()]
    if not flat:
        return Group(*out, Text("No tasks yet. Describe a goal and the planner will create them.", style=T.MUTED))
    for i, (depth, t) in enumerate(flat):
        sel = i == app.sel.get("tasks", 0)
        line = Text.assemble(("› " if sel else "  ", T.ACCENT), ("    " * depth, ""), T.status_mark(t["status"]), (" " + t["title"], T.TEXT if sel or t["status"] in ("active",) else T.MUTED))
        if t["req_ids"]:
            line.append("   " + " ".join(t["req_ids"]), style=T.DIM)
        out.append(line)
    return Group(*out)


def _vkind(v: str) -> str:
    v = v or ""
    return "command" if v.startswith("cmd:") else v or "—"


def requirements_view(app, w: int, h: int) -> Group:
    rq = app.rt.project.reqs
    sm = rq.summary()
    c = sm.counts
    narrow = w < 100
    head = Text.assemble(section("Requirements"), ("   ", ""), (f"{c['PASS']} pass", T.OK), (f"  {c['FAIL']} fail", T.FAIL if c["FAIL"] else T.DIM), (f"  {c['BLOCKED']} blocked", T.WARN if c["BLOCKED"] else T.DIM),
                         ("" if narrow else f"  {c['WAIVED']} waived  {c['NOT_STARTED'] + c['IN_PROGRESS']} open", T.DIM), (f"   {sm.resolved_pct:.0f}%" if narrow else f"   {sm.resolved_pct:.1f}% resolved", T.MUTED))
    out: list[Any] = [head, Text()]
    items = rq.all()
    if not items:
        return Group(*out, Text("No requirements yet — they are created from your request before any code is written.", style=T.MUTED))
    t = grid2((2, 8, None) if narrow else (2, 8, None, 14, 30), gap=2)
    sel_i = app.sel.get("requirements", 0)
    for i, r in enumerate(items):
        sel = i == sel_i
        row = [Text("›" if sel else " ", style=T.ACCENT), Text.assemble(T.status_mark(r["status"]), (" " + r["id"][4:], T.DIM)), Text(r["description"], style=T.TEXT if sel else T.MUTED)]
        if not narrow:
            row += [Text(_vkind(r["verify"]), style=T.DIM), Text(fit(r["result"] or "", 30), style=T.DIM)]
        t.add_row(*row)
    out.append(t)
    if items and 0 <= sel_i < len(items):
        r = items[sel_i]
        out += [Text(), section("Detail"), Text(f"{r['id']}  {r['description']}", style=T.TEXT), Text(f"source: {r['source']}   verify: {(r['verify'] or '—').replace('{py}', 'python')}", style=T.MUTED),
                Text(f"files: {', '.join(r['files']) or '—'}", style=T.MUTED), Text(f"result: {r['result'] or '—'}", style=T.MUTED)]
    return Group(*out)


STATE_STYLE = {"CONNECTED": T.OK, "LIVE VERIFIED": T.OK, "ERROR": T.FAIL, "READY FOR KEY": T.MUTED, "NOT CONFIGURED": T.DIM, "DISABLED": T.DIM,
               "NEEDS MODEL": T.WARN, "KEY SET · UNTESTED": T.WARN, "KEY SET · NOT ADDED": T.WARN, "UNTESTED": T.MUTED}
ACTIONS = [("a", "add key"), ("t", "test"), ("m", "select model"), ("p", "set primary"), ("f", "set fallback"), ("c", "configure"), ("x", "remove key")]


def models_view(app, w: int, h: int) -> Group:
    r = app.rt.router
    sts = app.pstatus
    usage = {u["provider"]: u for u in app.rt.gstore.usage_by_provider()}
    out: list[Any] = [Text.assemble(section("Models"), (f"   routing {r.mode.replace('_', ' ')}", T.MUTED))]
    out.append(Text())
    if not sts:
        return Group(*out, Text("Loading provider status…", style=T.DIM))
    narrow = w < 96
    t = grid2((2, 11, 16, 17, 7) if narrow else (2, 12, 24, 20, 8, 9, None), right=(4,) if narrow else (4, 5))
    t.add_row(Text(""), *[Text(x, style=f"bold {T.DIM}") for x in (("PROVIDER", "MODEL", "STATUS", "MS") if narrow else ("PROVIDER", "MODEL", "STATUS", "LATENCY", "TODAY", ""))])
    sel = min(app.sel.get("models", 0), len(sts) - 1)
    for i, s_ in enumerate(sts):
        u = usage.get(s_.name, {})
        tag = "primary" if s_.primary else "fallback" if s_.fallback else ""
        cells = [Text("›" if i == sel else " ", style=T.ACCENT), Text(s_.label, style=(T.TEXT if i == sel else T.AI) if s_.configured else (T.TEXT if i == sel else T.MUTED)),
                 Text(s_.model or "—", style=T.TEXT if s_.model else T.DIM), Text(("● " if s_.state in ("CONNECTED", "LIVE VERIFIED") else "") + s_.state, style=STATE_STYLE.get(s_.state, T.MUTED)),
                 Text(f"{s_.latency_ms}" if narrow and s_.latency_ms is not None else f"{s_.latency_ms}ms" if s_.latency_ms is not None else "—", style=T.MUTED)]
        if not narrow:
            cells += [Text(T.money(u["cost"] or 0) if u else "—", style=T.DIM), Text(tag, style=T.ACCENT)]
        t.add_row(*cells)
    out.append(t)
    cur = sts[sel]
    out += [Text(), Text.assemble((cur.label.upper() + "  ", f"bold {T.TEXT}"), (cur.state, STATE_STYLE.get(cur.state, T.MUTED)), (f"   {cur.detail}" if cur.detail else "", T.DIM))]
    out.append(Text.assemble(*[(f"{k} ", f"bold {T.MUTED}") if j % 2 == 0 else (f"{lbl}    ", T.DIM) for k, lbl in ACTIONS for j in (0, 1)]))
    verified = sum(1 for x in sts if x.live_verified)
    out += [Text(), Text.assemble(("EXTERNAL VERIFICATION  ", f"bold {T.DIM}"), (f"{verified} live-verified", T.OK if verified else T.DIM),
                                  ("  ·  READY FOR KEY = implemented + mock-tested; only a key is missing", T.DIM))]
    out += [Text(), section("Routing")]
    rt = grid2((16, 14, None))
    for role in ("planner", "implementer", "debugger", "reviewer", "ui_reviewer"):
        ex = r.route(role)
        rt.add_row(Text(role.replace("_", " "), style=T.MUTED), Text(ex.primary or "—", style=T.AI), Text(fit(ex.reason, w - 34), style=T.DIM))
    out.append(rt)
    return Group(*out)


def tools_view(app, w: int, h: int) -> Group:
    from ..tools import ToolBox
    tb = ToolBox(app.rt.project, app.rt.perms, app.rt.bus)
    stats = {r["tool"]: r for r in app.rt.project.store.query("SELECT tool, COUNT(*) n, AVG(duration) d, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) f FROM tool_calls WHERE status='done' GROUP BY tool")}
    t = grid2((16, 6, 8, 5, None), right=(1, 2, 3))
    t.add_row(*[Text(x, style=f"bold {T.DIM}") for x in ("TOOL", "CALLS", "AVG", "FAIL", "")])
    for n, sp in tb.specs.items():
        s = stats.get(n)
        t.add_row(Text(n, style=T.TEXT), Text(str(s["n"]) if s else "—", style=T.MUTED), Text(f"{s['d']:.2f}s" if s and s["d"] else "—", style=T.DIM), Text(str(s["f"]) if s and s["f"] else "", style=T.FAIL),
                  Text(fit("  " + sp.description, max(10, w - 44)), style=T.DIM))
    return Group(section("Tools", f"{len(tb.specs)} available"), Text(), t)


def tests_view(app, w: int, h: int) -> Group:
    p = app.rt.project
    out: list[Any] = [section("Quality gates"), Text()]
    t = grid2((12, 16, 10, None))
    for label, kind in (("Tests", "tests"), ("Build", "build"), ("Lint", "lint"), ("Types", "typecheck"), ("Browser", "browser")):
        r = p.last_run(kind)
        if not r:
            t.add_row(Text(label.upper(), style=T.DIM), Text("not run", style=T.DIM), Text(""), Text(""))
            continue
        detail = f"{r['passed']}/{r['total']}" if kind == "tests" else ("PASS" if r["ok"] else "FAIL")
        t.add_row(Text(label.upper(), style=T.DIM), Text(("✓ " if r["ok"] else "✕ ") + detail, style=T.OK if r["ok"] else T.FAIL), Text(ago(r["ts"]), style=T.DIM), Text(fit(r["summary"] or p.pretty(r["command"]), w - 44), style=T.MUTED))
    out.append(t)
    rows = p.store.query("SELECT * FROM test_runs WHERE kind='tests' ORDER BY id DESC LIMIT 8")
    if rows:
        out += [Text(), section("Recent test runs")]
        rt = grid2((10, 12, 8, None))
        for r in rows:
            rt.add_row(Text(time.strftime("%H:%M:%S", time.localtime(r["ts"])), style=T.DIM), Text(f"{r['passed']}/{r['total']}", style=T.OK if r["ok"] else T.FAIL), Text(f"{r['duration']:.1f}s", style=T.DIM), Text(fit(r["summary"] or "", w - 36), style=T.MUTED))
        out.append(rt)
    out += [Text(), hint(f"command: {p.pretty(p.info.test_cmd) or 'none detected'}    ·   run: ctrl+k → “Run tests”")]
    return Group(*out)


def preview_view(app, w: int, h: int) -> Group:
    procs = app.rt.project.procs.list()
    out: list[Any] = [section("Preview & processes"), Text()]
    if not procs:
        info = app.rt.project.info
        return Group(*out, Text("No processes running.", style=T.MUTED), Text(f"Dev command: {info.dev_cmd or 'none detected'}", style=T.DIM), Text("ctrl+k → “Start preview”", style=T.DIM))
    t = grid2((10, 9, 8, 7, 26, None))
    t.add_row(*[Text(x, style=f"bold {T.DIM}") for x in ("PROCESS", "STATUS", "PID", "PORT", "URL", "LOG")])
    for p in procs:
        t.add_row(Text(p["name"], style=T.TEXT), Text(p["status"], style=T.OK if p["status"] == "running" else T.DIM), Text(str(p["pid"]), style=T.DIM), Text(str(p["port"] or "—"), style=T.MUTED),
                  Text(p["url"] or "—", style=T.ACCENT), Text(fit(p["log"].replace(str(app.rt.project.root) + "/", ""), w - 66), style=T.DIM))
    out.append(t)
    first = next((p for p in procs if p["status"] == "running"), procs[0])
    out += [Text(), section("Log tail")] + [Text(fit(l, w - 2), style=T.MUTED) for l in app.rt.project.procs.tail(first["name"], max(4, h - 12)).splitlines()]
    return Group(*out)


def diff_view(app, w: int, h: int) -> Group:
    g = app.rt.project.git
    if not g.is_repo:
        return Group(section("Diff"), Text(), Text("Not a git repository — checkpoints will initialise one.", style=T.MUTED))
    now = time.time()
    if now - app.diff_cache[0] > 3:
        app.diff_cache = (now, g.numstat(), g.diff())
    _, stat, full = app.diff_cache
    add, rem = sum(a for _, a, _ in stat), sum(d for _, _, d in stat)
    out: list[Any] = [Text.assemble(section("Diff"), (f"   {len(stat)} files  ", T.MUTED), (f"+{add}", T.OK), ("  ", ""), (f"-{rem}", T.FAIL),
                                    ("   ↑↓ file · enter " + ("shows the selected file" if app.diff_all else "shows all files"), T.DIM)), Text()]
    if not stat:
        return Group(*out, Text("Working tree matches HEAD.", style=T.MUTED))
    sel = min(app.sel.get("diff", 0), len(stat) - 1)
    t = grid2((2, None, 7, 7, 16), right=(2, 3))
    mx = max(a + d for _, a, d in stat) or 1
    for i, (f, a, d) in enumerate(stat[:18]):
        n = max(1, int(14 * (a + d) / mx))
        pa = int(round(n * a / max(1, a + d)))
        t.add_row(Text("›" if i == sel else " ", style=T.ACCENT), Text(fit(f, w - 40), style=T.TEXT if i == sel else T.MUTED), Text(f"+{a}", style=T.OK if a else T.DIM), Text(f"-{d}", style=T.FAIL if d else T.DIM),
                  Text.assemble(("▪" * pa, T.OK), ("▪" * (n - pa), T.FAIL)))
    out += [t, Text()]
    if app.diff_all:
        out.append(render_diff(full))
    else:
        path = stat[sel][0]
        chunks = re.split(r"(?m)^(?=diff --git )", full)
        mine = next((c for c in chunks if c.startswith(f"diff --git a/{path} ") or f" b/{path}\n" in c.split("\n", 1)[0] + "\n"), "")
        out.append(render_diff(mine or full))
    return Group(*out)


def doctor_view(app, w: int, h: int) -> Group:
    rep = app.doctor_report
    if rep is None:
        return Group(section("Doctor"), Text(), Text("Running real checks…" if app.doctor_running else "Not run yet — ctrl+k → “Doctor”.", style=T.MUTED))
    pr = rep.problems
    out: list[Any] = [Text.assemble(section("Doctor"), (f"   {len(pr)} problem(s)" if pr else "   all clear", T.WARN if pr else T.OK), (f"   {rep.duration:.1f}s · every check ran for real", T.DIM))]
    glyph = {"ok": ("✓", T.OK), "fail": ("✕", T.FAIL), "warn": ("!", T.WARN), "skip": ("·", T.DIM)}
    for sec, cs in rep.sections.items():
        out += [Text(), Text(sec, style=f"bold {T.DIM}")]
        tb = grid2((2, 24, None))
        for c in cs:
            g_, col = glyph[c.status]
            tb.add_row(Text(g_, style=col), Text(c.name, style=T.TEXT if c.status != "skip" else T.MUTED), Text(fit(c.detail, w - 30), style=T.MUTED if c.status in ("ok", "skip") else col))
        out.append(tb)
    if pr:
        out += [Text(), section("Problems")] + [Text(f"  {c.name}: {fit(c.detail, w - 6)}" + ("   → genius doctor --fix" if c.fix else ""), style=T.MUTED) for c in pr]
    return Group(*out)


LOG_FILTERS = ["ALL", "AGENT", "TOOLS", "BUILD", "TESTS", "BROWSER", "MODEL", "ERRORS"]


def logs_view(app, w: int, h: int) -> Group:
    chips = Text()
    for i, f in enumerate(LOG_FILTERS, 1):
        on = f == app.log_filter
        chips.append(f"{i} {f}", style=f"bold {T.ACCENT}" if on else T.DIM)
        chips.append("    ")
    out: list[Any] = [chips]
    if app.log_query:
        out.append(Text(f"search: {app.log_query}   (esc clears)", style=T.MUTED))
    out.append(Text())
    evs = [e for e in app.rt.bus.buffer if (app.log_filter == "ALL" or e.category == app.log_filter) and (not app.log_query or app.log_query.lower() in (e.message + e.label + e.detail).lower())]
    n = max(5, h - 5)
    shown = evs[-n:]
    sel = min(app.sel.get("logs", len(shown) - 1), len(shown) - 1)
    if not shown:
        return Group(*out, Text("No matching events.", style=T.MUTED))
    for i, e in enumerate(shown):
        is_sel = i == sel
        row = Text.assemble(("› " if is_sel else "  ", T.ACCENT), (e.clock + "  ", T.DIM), (f"{e.label:9}", CAT_STYLE.get(e.category, T.MUTED)), (fit(e.message, w - 26) if id(e) not in app.expanded else e.message, T.TEXT if is_sel else T.MUTED))
        out.append(row)
        if id(e) in app.expanded and e.detail:
            out += [Text("      " + l[: w - 8], style=T.DIM) for l in e.detail.splitlines()[:24]]
    out += [Text(), hint("↑↓ select · enter expand · c copy · 1-8 filter · / search")]
    app.visible_logs = shown
    return Group(*out)


def checkpoints_view(app, w: int, h: int) -> Group:
    cps = app.rt.project.checkpoints.list()
    out: list[Any] = [section("Checkpoints", f"{len(cps)}   ·   enter restores the selected one (itself undoable)"), Text()]
    if not cps:
        return Group(*out, Text("No checkpoints yet — one is created automatically before every run.", style=T.MUTED))
    t = grid2((2, 8, 9, 10, 12, None))
    for i, c in enumerate(cps):
        t.add_row(Text("›" if i == app.sel.get("checkpoints", 0) else " ", style=T.ACCENT), Text(c["id"], style=T.ACCENT), Text(c["commit_hash"][:7], style=T.DIM), Text(ago(c["ts"]), style=T.MUTED),
                  Text(c["kind"], style=T.DIM), Text(fit(c["task"], w - 48), style=T.TEXT if i == app.sel.get("checkpoints", 0) else T.MUTED))
    return Group(*out, t)


def cost_view(app, w: int, h: int) -> Group:
    r = app.rt.router
    rows = app.rt.gstore.usage_by_provider()
    spent, limit = r.budget()
    out: list[Any] = [section("Cost · today"), Text()]
    if limit:
        pct = 100 * spent / limit
        out += [Text.assemble((T.money(spent), f"bold {T.TEXT}"), (f" of {T.money(limit)} daily budget", T.MUTED), (f"   {pct:.0f}%", T.WARN if pct >= 80 else T.DIM)), T.bar(pct, 40, T.WARN if pct >= 80 else T.ACCENT), Text()]
    t = grid2((14, 10, 9, 12, 12, 10, 8), right=(1, 2, 3, 4, 5, 6))
    t.add_row(*[Text(x, style=f"bold {T.DIM}") for x in ("PROVIDER", "COST", "REQS", "INPUT", "OUTPUT", "CACHED", "AVG")])
    for u in rows:
        t.add_row(Text(u["provider"], style=T.AI), Text(T.money(u["cost"] or 0), style=T.TEXT), Text(str(u["requests"]), style=T.MUTED), Text(f"{u['input_tokens'] or 0:,}", style=T.MUTED),
                  Text(f"{u['output_tokens'] or 0:,}", style=T.MUTED), Text(f"{u['cached_tokens'] or 0:,}", style=T.DIM), Text(f"{u['latency'] or 0:.1f}s", style=T.DIM))
    out.append(t)
    out += [Text(), Text.assemble(("TOTAL  ", T.DIM), (T.money(sum(u["cost"] or 0 for u in rows)), f"bold {T.TEXT}"))]
    byrole = app.rt.gstore.usage_grouped("role")
    if byrole:
        rt2 = grid2((18, 10, 9, 12), right=(1, 2, 3))
        for x in byrole[:8]:
            rt2.add_row(Text(str(x["key"]).replace("_", " "), style=T.MUTED), Text(T.money(x["cost"] or 0), style=T.TEXT), Text(str(x["requests"]), style=T.MUTED), Text(f"{(x['input_tokens'] or 0) + (x['output_tokens'] or 0):,} tok", style=T.DIM))
        out += [Text(), section("By agent role"), rt2]
    if any(not (u["cost"] or 0) for u in rows):
        out.append(hint("Providers at $0 have no pricing configured (input_cost / output_cost per 1M tokens)."))
    return Group(*out)


def project_view(app, w: int, h: int) -> Group:
    p = app.rt.project
    i = p.info
    s = app.snap
    t = grid2((16, None))
    for k, v in (("Path", str(p.root)), ("Kind", i.kind), ("Languages", ", ".join(i.languages) or "—"), ("Frameworks", ", ".join(i.frameworks) or "—"), ("Package mgr", ", ".join(i.package_managers) or "—"),
                 ("Databases", ", ".join(i.databases) or "—"), ("Services", ", ".join(i.services) or "—"), ("Test", p.pretty(i.test_cmd) or "—"), ("Build", p.pretty(i.build_cmd) or "—"), ("Lint", p.pretty(i.lint_cmd) or "—"),
                 ("Dev server", p.pretty(i.dev_cmd) or "—"), ("Git branch", s["branch"] or "not a repo"), ("Protected", ", ".join(app.rt.perms.protected) or "none")):
        t.add_row(Text(k.upper(), style=T.DIM), Text(fit(v, w - 22), style=T.TEXT))
    ix = s["index"]
    out: list[Any] = [section(p.name), Text(), t, Text(), section("Index"), Text(f"{ix['files']} files · {ix['symbols']} symbols · {ix['routes']} routes · {ix['tables']} tables · {ix['tests']} tests", style=T.MUTED), Text(), section("Memory (.genius)")]
    out += [Text(f"  {f.name}", style=T.MUTED) for f in sorted(p.gdir.iterdir()) if f.is_file()]
    return Group(*out)


def output_view(app, w: int, h: int) -> Group:
    title = app.result_title or "Result"
    return Group(section(title), Text(), Text(app.last_result or "Nothing to show yet.", style=T.TEXT if app.last_result else T.MUTED))


# ------------------------------------------------------------------------------------------------ SETTINGS
def settings_items(app) -> list[tuple[str, str, str, str, Any]]:
    """(section, key, label, kind, options)"""
    from ..router import MODES
    provs = list(app.rt.router.configs())
    items: list[tuple[str, str, str, str, Any]] = [("Routing", "general.routing_mode", "Routing mode", "choice", MODES)]
    for role in ("planner", "implementer", "debugger", "reviewer"):
        items.append(("Routing", f"routing.custom.{role}", f"{role.capitalize()} model", "choice", ["auto"] + provs))
    for n in provs:
        items.append(("Models", f"providers.{n}.enabled", f"{n} enabled", "bool", None))
        items.append(("Models", f"providers.{n}.tier", f"{n} strength tier (1 economy – 3 strongest)", "choice", [1, 2, 3]))
    items += [
        ("Project", "general.primary", "Primary model (all roles)", "choice", ["auto"] + provs),
        ("Permissions", "general.permission", "Permission level", "choice", ["safe", "standard", "autonomous"]),
        ("Budget", "general.daily_budget", "Daily budget (USD, 0 = unlimited)", "number", (0, 500, 0.5)),
        ("Tools", "tools.shell_timeout", "Shell timeout (seconds)", "number", (10, 3600, 30)),
        ("Browser", "browser.headless", "Headless browser", "bool", None),
        ("Git", "git.auto_init", "Initialise git for checkpoints", "bool", None),
        ("Git", "git.auto_commit", "Auto-commit (never pushes)", "bool", None),
        ("Privacy", "privacy.cloud_allowed", "Allow cloud models", "bool", None),
        ("Privacy", "privacy.redact_secrets", "Redact secrets before sending", "bool", None),
        ("Privacy", "privacy.max_external_context_chars", "Max context sent externally (chars)", "number", (10000, 1000000, 10000)),
        ("UI", "ui.animations", "Animations", "bool", None),
        ("Advanced", "plugins.trust_project", "Trust project-local plugins", "bool", None),
        ("Advanced", "tools.max_output_chars", "Tool output kept for the model (chars)", "number", (1000, 50000, 1000)),
    ]
    return items


def settings_view(app, w: int, h: int) -> Group:
    items = settings_items(app)
    cfg = app.rt.project.cfg
    app.sel_line = None
    out: list[Any] = [section("Settings", "↑↓ select · ←→ change · edits apply immediately"), Text()]
    cur = None
    sel = min(app.sel.get("settings", 0), len(items) - 1)
    t: Table | None = None
    for i, (sec, key, label, kind, opts) in enumerate(items):
        if sec != cur:
            if t is not None:
                out.append(t)
            out += [Text(), Text(sec.upper(), style=f"bold {T.DIM}")]
            t = grid2((2, 46, None))
            cur = sec
        assert t is not None
        v = cfg.get(key, "auto" if key.startswith("routing.custom") else None)
        if (key.startswith("routing.custom") or key == "general.primary") and not v:
            v = "auto"
        if kind == "bool":
            val = Text("● on", style=T.ACCENT) if v else Text("○ off", style=T.DIM)
        elif kind == "number":
            val = Text(f"{v:g}" if isinstance(v, (int, float)) else str(v), style=T.TEXT)
        else:
            val = Text(str(v), style=T.AI if key.startswith(("routing.custom", "providers", "general.primary")) and v != "auto" else T.TEXT)
        sel_row = i == sel
        if sel_row:
            app.sel_line = 4 + i + 3 * len({x[0] for x in items[: i + 1]})
        t.add_row(Text("›" if sel_row else " ", style=T.ACCENT), Text(label, style=T.TEXT if sel_row else T.MUTED), val)
    if t is not None:
        out.append(t)
    out += [Text(), hint("Provider credentials: `genius models add` / `genius models key <name>` · full config: `genius config`")]
    return Group(*out)


VIEW_FUNCS = {"home": home, "tasks": tasks_view, "requirements": requirements_view, "models": models_view, "tools": tools_view, "tests": tests_view, "preview": preview_view,
              "diff": diff_view, "doctor": doctor_view, "logs": logs_view, "checkpoints": checkpoints_view, "cost": cost_view, "project": project_view, "settings": settings_view, "output": output_view}
