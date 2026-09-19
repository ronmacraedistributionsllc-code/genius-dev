"""Genius Dev TUI (Textual). Layout: header · rule · single content view · rule · prompt · key hints."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Input, Rule, Static

from .. import theme as T
from ..controller import Controller
from ..runtime import Runtime, build_runtime, recover_crashed_state
from ..status import snapshot
from ..tools import ToolBox
from .views import ACTIONS, LOG_FILTERS, VIEW_FUNCS, VIEW_TITLES, settings_items
from .widgets import fit

DEFAULT_KEYS = {"palette": "ctrl+k", "tasks": "ctrl+p", "logs": "ctrl+l", "tests": "ctrl+t", "diff": "ctrl+d", "preview": "ctrl+v",
                "models": "ctrl+o", "resume": "ctrl+r", "stop": "ctrl+x", "home": "escape"}
SELECTABLE = {"tasks", "requirements", "models", "logs", "checkpoints", "settings", "diff"}

CSS = f"""
Screen {{ background: {T.BG}; color: {T.TEXT}; }}
#hdr {{ height: 2; padding: 0 3; margin: 1 0 0 0; }}
Rule {{ color: {T.RULE}; margin: 0 3; height: 1; }}
#main {{ padding: 1 3 0 3; scrollbar-size-vertical: 1; scrollbar-color: {T.RULE}; scrollbar-color-hover: {T.DIM}; scrollbar-background: {T.BG}; scrollbar-background-hover: {T.BG}; }}
#content {{ width: 100%; height: auto; }}
#promptrow {{ height: 1; padding: 0 3; margin: 1 0 0 0; }}
#caret {{ width: 2; color: {T.ACCENT}; text-style: bold; }}
Input {{ border: none; background: transparent; padding: 0; height: 1; color: {T.TEXT}; }}
Input:focus {{ border: none; background: transparent; }}
Input > .input--placeholder {{ color: {T.DIM}; }}
Input > .input--cursor {{ background: {T.ACCENT}; color: {T.BG}; }}
#footer {{ height: 1; padding: 0 3; margin: 0 0 1 0; color: {T.DIM}; }}
ApprovalScreen {{ align: center middle; background: {T.BG} 70%; }}
#dialog {{ width: 70; max-width: 92%; height: auto; max-height: 90%; padding: 1 3; background: {T.SURFACE}; border: round {T.RULE}; }}
PromptScreen {{ align: center middle; background: {T.BG} 70%; }}
#dlg-input {{ margin: 1 0; background: {T.BG}; padding: 0 1; }}
#dlg-body {{ max-height: 22; height: auto; }}
"""

THEME = Theme(name="genius", primary=T.ACCENT, secondary=T.AI, accent=T.ACCENT, foreground=T.TEXT, background=T.BG, surface=T.SURFACE, panel=T.SURFACE,
              success=T.OK, warning=T.WARN, error=T.FAIL, dark=True)


class ApprovalScreen(ModalScreen[bool]):
    BINDINGS = [("y", "answer(True)", "Allow"), ("enter", "answer(True)", "Allow"), ("n", "answer(False)", "Deny"), ("escape", "answer(False)", "Deny"),
                ("down", "scroll(1)", "Scroll"), ("up", "scroll(-1)", "Scroll")]

    def __init__(self, title: str, reason: str, danger: bool = False):
        super().__init__()
        self.title_text, self.reason, self.danger = title, reason, danger

    def compose(self) -> ComposeResult:
        head = Text()
        head.append("PERMISSION NEEDED\n\n", style=f"bold {T.WARN}")
        head.append(fit(self.title_text, 62) + "\n", style=f"bold {T.TEXT}")
        body = Text()
        for line in self.reason.splitlines():                        # diff lines from the proposed change are coloured
            style = T.OK if line.startswith("+") else T.FAIL if line.startswith("-") else T.DIM if line.startswith(("@@", "…")) else T.MUTED
            body.append(fit(line, 66) + "\n", style=style)
        foot = Text()
        foot.append("y", style=f"bold {T.ACCENT}"); foot.append(" allow      ", style=T.MUTED)
        foot.append("n", style=f"bold {T.ACCENT}"); foot.append(" deny      ", style=T.MUTED)
        foot.append("↑↓ scroll", style=T.DIM)
        with Vertical(id="dialog"):
            yield Static(head)
            with VerticalScroll(id="dlg-body"):
                yield Static(body)
            yield Static(Text("\n") + foot)

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)

    def action_scroll(self, d: int) -> None:
        self.query_one("#dlg-body").scroll_relative(y=d * 2, animate=False)


class PromptScreen(ModalScreen):
    """Small input dialog (hidden input for secrets). Dismisses with the text, or None when cancelled."""
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, title: str, hint: str = "", initial: str = "", password: bool = False):
        super().__init__()
        self.title_text, self.hint, self.initial, self.password = title, hint, initial, password

    def compose(self) -> ComposeResult:
        t = Text()
        t.append(self.title_text.upper() + "\n", style=f"bold {T.TEXT}")
        if self.hint:
            t.append(self.hint + "\n", style=T.MUTED)
        with Vertical(id="dialog"):
            yield Static(t)
            yield Input(value=self.initial, password=self.password, id="dlg-input")
            yield Static(Text("enter confirm   esc cancel" + ("   ·   input is hidden and never displayed" if self.password else ""), style=T.DIM))

    def on_mount(self) -> None:
        self.query_one("#dlg-input", Input).focus()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        ev.stop()
        self.dismiss(ev.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class GeniusCommands(Provider):
    async def discover(self) -> Hits:
        for title, help_, cb in self.app.palette_items()[:9]:  # type: ignore[attr-defined]
            yield DiscoveryHit(title, cb, help=help_)

    async def search(self, query: str) -> Hits:
        m = self.matcher(query)
        for title, help_, cb in self.app.palette_items():  # type: ignore[attr-defined]
            score = m.match(title)
            if score > 0:
                yield Hit(score, m.highlight(title), cb, help=help_)


class SearchCommands(Provider):
    """Global fuzzy search across files, symbols, routes, tables, requirements and tasks."""

    async def search(self, query: str) -> Hits:
        if len(query) < 2:
            return
        app: GeniusApp = self.app  # type: ignore[assignment]
        ix = app.rt.project.index
        from ..semantic import semantic_search
        for h in semantic_search(ix, query, 6):
            yield Hit(min(0.95, 0.5 + h["score"] / 40), f"{h['kind']}  {h['title']}", lambda h=h: app.open_file(h["path"], h["line"]), help=f"{h['path']}:{h['line']}  ·  semantic")
        for fh in ix.search(query, 10):
            yield Hit(min(0.99, fh.score / 110), f"{fh.kind}  {fh.text}", lambda fh=fh: app.open_file(fh.path, fh.line), help=f"{fh.path}:{fh.line}" if fh.line else "")
        for r in app.rt.project.reqs.all():
            if query.lower() in r["description"].lower() or query.lower() == r["id"].lower():
                yield Hit(0.8, f"{r['id']}  {r['description'][:60]}", lambda: app.set_view("requirements"), help=r["status"])
        for _, t in app.rt.project.tasks.flat():
            if query.lower() in t["title"].lower():
                yield Hit(0.7, f"task  {t['title'][:60]}", lambda: app.set_view("tasks"), help=t["status"])


class GeniusApp(App):
    CSS = CSS
    COMMANDS = {GeniusCommands, SearchCommands}
    COMMAND_PALETTE_BINDING = "ctrl+k"
    BINDINGS = []  # type: ignore[assignment]
    ENABLE_COMMAND_PALETTE = True

    def __init__(self, rt: Runtime, initial: str | None = None):
        super().__init__(ansi_color=False)
        self.rt = rt
        self.ctl = Controller(rt, self.show_result, self.set_view)
        self.ctl.on_doctor = self.show_doctor
        rt.perms.ask = self.ask_permission
        self.initial = initial
        self.view = "home"
        self.sel: dict[str, int] = {}
        self.log_filter, self.log_query = "ALL", ""
        self.expanded: set[int] = set()
        self.visible_logs: list = []
        self.health: dict[str, tuple[bool, float, str]] = {}
        self.sel_line: int | None = None
        self.pstatus: list = []
        self.doctor_report: Any = None
        self.doctor_running = False
        self.diff_all = False
        self.last_result = ""
        self.result_title = ""
        self.diff_cache: tuple[float, list, str] = (0.0, [], "")
        self.snap: dict[str, Any] = snapshot(rt)
        self._snap_at = 0.0
        self._dirty = True
        self._last_render = 0.0
        self.history: list[str] = []
        self.hist_i = 0
        self.started_at = time.time()
        keys = {**DEFAULT_KEYS, **(rt.project.cfg.get("ui.keys", {}) or {})}
        self.keymap = keys
        for action, key in keys.items():
            if action == "palette":
                continue
            self._bindings.bind(key, {"tasks": "view('tasks')", "logs": "view('logs')", "tests": "view('tests')", "diff": "view('diff')", "preview": "view('preview')",
                            "models": "view('models')", "resume": "resume", "stop": "stop", "home": "back"}[action], show=False, priority=True)
        for k, a in (("up", "move(-1)"), ("down", "move(1)"), ("left", "change(-1)"), ("right", "change(1)"), ("pageup", "page(-1)"), ("pagedown", "page(1)")):
            self._bindings.bind(k, a, show=False, priority=True)
        rt.bus.subscribe(lambda ev: setattr(self, "_dirty", True))

    # ---- layout ----------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="hdr")
        yield Rule()
        with VerticalScroll(id="main"):
            yield Static(id="content")
        yield Rule()
        with Horizontal(id="promptrow"):
            yield Static("›", id="caret")
            yield Input(placeholder="Describe what to build or fix…", id="prompt")
        yield Static(id="footer")

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "genius"
        self.query_one("#prompt", Input).focus()
        self.set_interval(0.2, self.tick)
        self.run_worker(self.startup(), exclusive=False)
        self.render_all()
        if self.initial:
            self.call_later(lambda: self.submit(self.initial))

    async def startup(self) -> None:
        bus, p = self.rt.bus, self.rt.project
        bus.emit("SYSTEM", "LOAD", "Loading project…")
        stats = await asyncio.to_thread(p.index.refresh)
        bus.emit("SYSTEM", "INDEX", f"Repository index ready · {stats['files']} files")
        sm = p.reqs.summary()
        if sm.total:
            bus.emit("SYSTEM", "REQS", f"{sm.total} requirements" + (f" · {sm.counts['FAIL']} failing from last session" if sm.counts["FAIL"] else ""))
        recover_crashed_state(p, bus)
        r = self.rt.router
        route = r.route("implementer")
        if not route.chain:
            bus.emit("ERRORS", "MODEL", "No model configured — run `genius models add <provider>` or try `genius demo`")
        else:
            from ..cli_probe import probe_providers
            probes = await probe_providers(r, route.chain[:2], record=False, local_only=True)          # only local/in-process providers are contacted at startup
            for name in route.chain[:2]:
                if name in probes:
                    ok, lat, detail = probes[name]
                    self.health[name] = probes[name]
                    bus.emit("MODEL" if ok else "ERRORS", "MODEL", f"{name} connected · {lat * 1000:.0f}ms" if ok else f"{name} unavailable — {detail}")
                else:
                    bus.emit("MODEL", "MODEL", f"{name} selected — not contacted until first use (cloud)")
        await self.refresh_statuses()
        bus.emit("SYSTEM", "READY", "Ready.")

    # ---- rendering -------------------------------------------------------------------
    def tick(self) -> None:
        now = time.time()
        st = (self.rt.agent.state["status"], self.ctl.busy)
        if st != getattr(self, "_last_st", None):
            self._last_st, self._dirty, self._snap_at = st, True, 0.0          # state transition: refresh immediately
        running = st[0] in ("running", "paused")
        if self._dirty or (running and self.view == "home") or now - self._last_render > 1.0:
            self.render_all()

    def render_all(self) -> None:
        now = time.time()
        self._dirty, self._last_render = False, now
        if now - self._snap_at > 0.4:
            self.snap, self._snap_at = snapshot(self.rt), now
        main = self.query_one("#main")
        w, h = max(40, main.size.width), max(8, main.size.height)
        try:
            self.query_one("#content", Static).update(VIEW_FUNCS[self.view](self, w, h))
        except Exception as e:  # noqa: BLE001 — a render bug must never take the UI down
            self.query_one("#content", Static).update(Text(f"view error: {type(e).__name__}: {e}", style=T.FAIL))
        if self.sel_line is not None:
            y0 = int(main.scroll_offset.y)
            if self.sel_line > y0 + h - 4:
                main.scroll_to(y=self.sel_line - h + 6, animate=False)
            elif self.sel_line < y0 + 2:
                main.scroll_to(y=max(0, self.sel_line - 3), animate=False)
        self.query_one("#hdr", Static).update(self.header(self.size.width - 6))
        self.query_one("#footer", Static).update(self.footer(self.size.width - 6))
        inp = self.query_one("#prompt", Input)
        busy = self.ctl.busy
        inp.placeholder = "Working — type stop, pause, or give another instruction…" if busy else "Describe what to build or fix…"

    def header(self, w: int) -> Text:
        s, a = self.snap, self.snap["agent"]
        st = a["status"]
        dot, word, col = (("●" if int(time.time() * 2) % 2 == 0 else "○", "ACTIVE", T.ACCENT) if st == "running" else ("⏸", "PAUSED", T.WARN) if st == "paused" else ("●", "IDLE", T.DIM))
        right1 = Text.assemble((s["permission"].upper() + " ", f"bold {T.MUTED}"), (dot + " ", col), (word, col))
        title = "GENIUS DEV" + ("" if self.view == "home" else f"  ·  {VIEW_TITLES[self.view]}")
        left1 = Text(title, style=f"bold {T.TEXT}")
        cfgs = self.rt.router.configs()
        offline = bool(s["model"] and s["model"] in cfgs and cfgs[s["model"]].kind == "mock")
        model = Text.assemble((s["model"] or "no model", T.AI if s["model"] else T.WARN), ("  offline", T.DIM) if offline else ("", ""), (f"  →  {s['fallback']}", T.AI) if s["fallback"] else ("", ""))
        branch = f"  ·  {s['branch']}" if s["branch"] else ""
        left2 = Text(fit(f"{s['project']}{branch}", max(10, w - model.cell_len - 3)), style=T.MUTED)

        def line(l: Text, r: Text) -> Text:
            pad = max(2, w - l.cell_len - r.cell_len)
            return Text.assemble(l, " " * pad, r)
        return Text("\n").join([line(left1, right1), line(left2, model)])

    def footer(self, w: int) -> Text:
        k = self.keymap
        pretty = lambda x: x.replace("ctrl+", "^").replace("escape", "esc")
        items = [(pretty(k["palette"]), "commands"), (pretty(k["tasks"]), "tasks"), (pretty(k["logs"]), "logs"), (pretty(k["tests"]), "tests"), (pretty(k["diff"]), "diff"),
                 (pretty(k["models"]), "models"), (pretty(k["home"]), "home")]
        if self.ctl.busy:
            items.insert(1, (pretty(k["stop"]), "stop"))
        t = Text()
        for key, name in items:
            seg = len(key) + len(name) + 4
            if t.cell_len + seg > w:
                break
            t.append(key, style=T.MUTED)
            t.append(f" {name}    ", style=T.DIM)
        return t

    # ---- actions ---------------------------------------------------------------------
    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if self.screen.is_modal or type(self.screen).__name__ == "CommandPalette":
            return False
        inp = self.query_one("#prompt", Input)
        if action == "change":
            return self.view == "settings" and not inp.value
        return True

    def set_view(self, view: str) -> None:
        self.view = view
        self.sel.setdefault(view, 0)
        if view == "logs":
            self.sel["logs"] = 10**6
        if view == "models":
            self.run_worker(self.refresh_statuses())
        self._dirty = True
        self.query_one("#main").scroll_home(animate=False)
        self.render_all()

    def action_view(self, name: str) -> None:
        self.set_view(name)

    def action_back(self) -> None:  # type: ignore[override]
        if self.log_query:
            self.log_query = ""
            self.query_one("#prompt", Input).value = ""
        elif self.view != "home":
            self.set_view("home")

    def action_stop(self) -> None:
        self.ctl.rt.agent.stop()
        self.rt.bus.emit("AGENT", "STOP", "Stopping after the current step…")

    def action_resume(self) -> None:
        self.submit("continue where you stopped")

    def action_page(self, d: int) -> None:
        m = self.query_one("#main")
        m.scroll_page_down() if d > 0 else m.scroll_page_up()

    def _count(self) -> int:
        p = self.rt.project
        return {"tasks": len(p.tasks.flat()), "requirements": len(p.reqs.all()), "models": len(self.pstatus) or len(self.rt.router.configs()), "logs": len(self.visible_logs),
                "checkpoints": len(p.checkpoints.list()), "settings": len(settings_items(self)), "diff": len(self.diff_cache[1]) or 1}.get(self.view, 0)

    def action_move(self, d: int) -> None:
        inp = self.query_one("#prompt", Input)
        if self.view in SELECTABLE:
            n = self._count()
            cur = min(self.sel.get(self.view, 0), max(0, n - 1))
            self.sel[self.view] = max(0, min(n - 1, cur + d))
            self.render_all()
            return
        if self.history:                                      # prompt history elsewhere
            self.hist_i = max(0, min(len(self.history), self.hist_i + d))
            inp.value = self.history[self.hist_i] if self.hist_i < len(self.history) else ""
            inp.cursor_position = len(inp.value)
        elif d:
            self.query_one("#main").scroll_relative(y=d * 3, animate=False)

    def action_change(self, d: int) -> None:
        if self.view == "settings":
            self.change_setting(d)

    # ---- settings ----------------------------------------------------------------------
    def change_setting(self, d: int) -> None:
        items = settings_items(self)
        sec, key, label, kind, opts = items[min(self.sel.get("settings", 0), len(items) - 1)]
        cfg = self.rt.project.cfg
        cur = cfg.get(key)
        if key == "general.primary" and not cur:
            cur = "auto"
        if kind == "bool":
            new: Any = not bool(cur)
        elif kind == "number":
            lo, hi, step = opts
            new = round(max(lo, min(hi, (cur or 0) + d * step)), 4)
            new = int(new) if isinstance(step, int) else new
        else:
            cur = cur if cur not in (None, "") else "auto"
            i = opts.index(cur) if cur in opts else 0
            new = opts[(i + d) % len(opts)]
            if key.startswith("routing.custom") or key == "general.primary":
                new = "" if new == "auto" else new
        proj_has = _has(cfg._project, key)
        cfg.set(key, new, "project" if proj_has else "global")
        if key == "general.permission":
            self.rt.perms.mode = new
        if key == "general.routing_mode":
            self.rt.router.mode_override = None
        if key.startswith(("providers.", "routing.", "general.primary")):
            self.rt.router.refresh()
            self.run_worker(self.refresh_statuses())
        self._dirty = True
        self.render_all()

    # ---- prompt -------------------------------------------------------------------------
    def on_input_changed(self, ev: Input.Changed) -> None:
        v = ev.value
        if self.view == "models" and len(v) == 1 and v in "atmpfcx":
            ev.input.value = ""
            self.run_worker(self.provider_action(v))
            return
        if self.view == "logs":
            if len(v) == 1 and v in "12345678":
                self.log_filter = LOG_FILTERS[int(v) - 1]
                ev.input.value = ""
                self.sel["logs"] = 10**6
                self.render_all()
            elif v.strip().lower() == "c" and self.visible_logs:
                sel = min(self.sel.get("logs", 0), len(self.visible_logs) - 1)
                e = self.visible_logs[sel]
                self.copy_to_clipboard(f"{e.clock} {e.label} {e.message}\n{e.detail}")
                self.notify("Copied", timeout=1.5)
                ev.input.value = ""
            elif v.startswith("/"):
                self.log_query = v[1:]
                self.render_all()

    async def on_input_submitted(self, ev: Input.Submitted) -> None:
        text = ev.value.strip()
        ev.input.value = ""
        if not text:
            self.run_worker(self.activate(), exclusive=False)     # never await dialogs inside a message handler (would deadlock the pump)
            return
        if self.view == "logs" and text.startswith("/"):
            return
        self.submit(text)

    def submit(self, text: str) -> None:
        self.history.append(text)
        self.hist_i = len(self.history)
        if self.view != "home" and self.view not in ("output",) and not text.startswith("/"):
            pass
        self.rt.bus.emit("AGENT", "YOU", text)
        self.run_worker(self.ctl.handle(text), exclusive=False)
        self._dirty = True

    async def activate(self) -> None:
        v = self.view
        if v == "logs" and self.visible_logs:
            e = self.visible_logs[min(self.sel.get("logs", 0), len(self.visible_logs) - 1)]
            self.expanded.symmetric_difference_update({id(e)})
        elif v == "checkpoints":
            cps = self.rt.project.checkpoints.list()
            if cps:
                cp = cps[min(self.sel.get("checkpoints", 0), len(cps) - 1)]
                if await self.ask_permission(f"Restore {cp['id']}?", f"“{cp['task'][:50]}” — files will change; the current state is checkpointed first so you can undo."):
                    await self.ctl.dispatch(__import__("genius_dev.intent", fromlist=["Intent"]).Intent("restore", {"id": cp["id"]}))
        elif v == "models":
            self.run_worker(self.provider_action("t"))
        elif v == "diff":
            self.diff_all = not self.diff_all
        elif v == "settings":
            self.change_setting(1)
        self.render_all()

    # ---- results / dialogs --------------------------------------------------------------
    def show_result(self, title: str, body: str) -> None:
        self.last_result, self.result_title = body, title
        if title != "Result" or self.view not in ("home",):
            self.set_view("output")
        self._dirty = True

    async def ask_permission(self, title: str, reason: str) -> bool:
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.push_screen(ApprovalScreen(title, reason), lambda ok: fut.set_result(bool(ok)))
        return await fut

    def open_file(self, path: str, line: int = 0) -> None:
        p = self.rt.project.root / path
        try:
            lines = p.read_text(errors="replace").splitlines()
        except OSError:
            lines = ["(unreadable)"]
        lo = max(0, line - 6)
        self.last_result = "\n".join(f"{i:>4}  {l}" for i, l in enumerate(lines[lo: lo + 80], lo + 1))
        self.result_title = f"{path}:{line}" if line else path
        self.set_view("output")

    async def test_models(self) -> None:
        r = self.rt.router
        self.rt.bus.emit("MODEL", "TEST", "Testing providers…")
        async def one(n: str) -> None:
            self.health[n] = await r.provider(n).health_check()
            ok, lat, d = self.health[n]
            self.rt.bus.emit("MODEL" if ok else "ERRORS", "TEST", f"{n}: {'ok' if ok else d} ({lat * 1000:.0f}ms)")
        await asyncio.gather(*[one(n) for n, c in r.configs().items() if c.enabled])

    # ---- provider management --------------------------------------------------------------
    async def refresh_statuses(self, probe_names: list[str] | None = None, record: bool = False, local_only: bool = True) -> None:
        from ..cli_probe import probe_providers
        from ..providers.status import statuses
        self.rt.project.cfg.reload()
        probes = await probe_providers(self.rt.router, probe_names, record=record, local_only=local_only)
        self.pstatus = statuses(self.rt.project.cfg, self.rt.gstore, probes)
        self._dirty = True

    def _selected_provider(self):
        if not self.pstatus:
            return None
        return self.pstatus[min(self.sel.get("models", 0), len(self.pstatus) - 1)]

    async def provider_action(self, key: str, name: str | None = None) -> None:
        """Keyboard actions of the Models screen (also reachable from the palette)."""
        from ..config import PRESETS, Config, ProviderConfig
        from ..providers.status import clear_live
        from ..secrets import has_key, keychain_delete, keychain_set
        st = next((x for x in self.pstatus if x.name == name), None) if name else self._selected_provider()
        if st is None:
            return
        n, cfg, say = st.name, Config(None), (lambda m, sev="information": self.notify(m, severity=sev, timeout=4))
        def ensure() -> ProviderConfig:
            have = cfg.providers()
            if n in have:
                return have[n]
            p = ProviderConfig.from_dict(n, PRESETS[n])
            cfg.save_provider(p)
            return p
        if key == "a":                                                          # ADD KEY
            p = ensure()
            if p.api_key_ref == "none":
                say(f"{st.label} does not use an API key"); return
            secret = await self.push_screen_wait_(PromptScreen(f"Add key · {st.label}", "Stored in the macOS Keychain. Nothing you type is shown or logged.", password=True))
            if not secret:
                return
            if not keychain_set(n, secret.strip()):
                say("macOS Keychain unavailable — set an environment variable instead", "error"); return
            del secret
            p.api_key_ref = f"keychain:{n}"; cfg.save_provider(p); clear_live(self.rt.gstore, n)
            say(f"{st.label}: Configured: YES — press t to test")
        elif key == "t":                                                        # TEST
            if not st.configured or (st.state == "READY FOR KEY"):
                say(f"{st.label}: READY FOR KEY — press a to add a key first"); return
            self.rt.router.refresh()
            await self.refresh_statuses([n], record=True, local_only=False)
            new = next(x for x in self.pstatus if x.name == n)
            say(f"{st.label}: {new.state}" + (f" · {new.latency_ms}ms" if new.latency_ms is not None else "") + (f" — {new.detail}" if new.detail else ""), "information" if new.state in ("CONNECTED", "LIVE VERIFIED") else "warning")
            return
        elif key == "m":                                                        # SELECT MODEL
            p = ensure()
            hint = "Model ID to use for this provider."
            if p.api_key_ref == "none" or has_key(p.api_key_ref):
                self.rt.router.refresh()
                prov = self.rt.router.provider(n)
                ids = await prov.list_models() if hasattr(prov, "list_models") else []
                if ids:
                    hint = "Available: " + ", ".join(ids[:6]) + (" …" if len(ids) > 6 else "")
            mid = await self.push_screen_wait_(PromptScreen(f"Select model · {st.label}", hint, p.model))
            if mid is None or not mid.strip():
                return
            p.model = mid.strip(); cfg.save_provider(p)
            say(f"{st.label} → {p.model}")
        elif key == "p":                                                        # SET PRIMARY
            ensure(); cfg.set("general.primary", n)
            say(f"{st.label} is now the primary provider")
        elif key == "f":                                                        # SET / UNSET FALLBACK
            ensure()
            chain = list(cfg.get("fallback.chain", []) or [])
            chain = [c for c in chain if c != n] if n in chain else chain + [n]
            cfg.set("fallback.chain", chain)
            say(("Fallback order: " + " → ".join(chain)) if chain else "Fallback cleared")
        elif key == "c":                                                        # CONFIGURE
            p = ensure()
            for label, attr, cast in (("Base URL", "base_url", str), ("Context window (tokens)", "context_window", int), ("Input price USD per 1M tokens", "input_cost", float), ("Output price USD per 1M tokens", "output_cost", float)):
                v = await self.push_screen_wait_(PromptScreen(f"Configure · {st.label}", label, str(getattr(p, attr))))
                if v is None:
                    break
                try:
                    setattr(p, attr, cast(v.strip()))
                except ValueError:
                    say(f"{label}: not a valid value", "error"); break
            cfg.save_provider(p)
            say(f"{st.label} updated")
        elif key == "x":                                                        # REMOVE KEY
            if not st.key_present:
                say(f"{st.label}: no stored key"); return
            if not await self.ask_permission(f"Remove the {st.label} key?", "It is deleted from the macOS Keychain. You can add it again at any time."):
                return
            keychain_delete(n)
            if n in cfg.providers() and n in PRESETS:
                p = cfg.providers()[n]; p.api_key_ref = PRESETS[n]["api_key_ref"]; cfg.save_provider(p)
            clear_live(self.rt.gstore, n)
            say(f"{st.label}: Configured: NO")
        self.rt.router.refresh()
        await self.refresh_statuses()

    async def push_screen_wait_(self, screen):
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.push_screen(screen, lambda v: fut.set_result(v))
        return await fut

    # ---- doctor ------------------------------------------------------------------------------
    async def run_doctor_view(self) -> None:
        from ..doctor import run_doctor
        self.doctor_running = True
        self.set_view("doctor")
        try:
            self.doctor_report = await run_doctor(self.rt.project, self.rt.router, True)
        finally:
            self.doctor_running = False
        self._dirty = True

    def show_doctor(self, rep) -> None:
        self.doctor_report = rep
        self.set_view("doctor")

    # ---- palette -------------------------------------------------------------------------
    def palette_items(self) -> list[tuple[str, str, Any]]:
        v = lambda name: (lambda: self.set_view(name))
        run = lambda coro_fn: (lambda: self.run_worker(coro_fn()))
        say = lambda text: (lambda: self.submit(text))
        items: list[tuple[str, str, Any]] = [
            ("Resume last session", "reconstruct state and continue", self.action_resume),
            ("Run tests", "detected test command", run(self._gate("tests"))),
            ("Run build", "detected build command", run(self._gate("build"))),
            ("Doctor", "real system, project, model and quality checks", run(self.run_doctor_view)),
            ("Finish audit", "deep completion audit and repair", say("Find everything preventing this app from being production ready and fix it")),
            ("Review changes", "review the current diff", say("review")),
            ("Security scan", "static security checks", say("security")),
            ("Create checkpoint", "snapshot the working tree", say("checkpoint")),
            ("Undo / restore last checkpoint", "revert the last run", say("restore last checkpoint")),
            ("Write handoff", "portable package for another agent", say("handoff")),
            ("Start preview", "start dev server", run(self._preview)),
            ("Stop preview", "stop dev server", lambda: (self.rt.project.procs.stop_all(), self.render_all())),
            ("Test models", "health-check every provider", run(self.test_models)),
            ("Stop agent", "halt after current step", self.action_stop),
            ("Pause agent", "", say("pause")), ("Resume agent", "", say("resume")),
        ]
        for name in ("home", "tasks", "requirements", "models", "tools", "tests", "preview", "diff", "doctor", "logs", "checkpoints", "cost", "project", "settings", "output"):
            items.append((f"Go to {VIEW_TITLES[name]}", "view", v(name)))
        for mode in ("economy", "balanced", "max_quality", "local_only"):
            items.append((f"Routing mode: {mode.replace('_', ' ')}", "model routing", say(f"mode {mode}")))
        for perm in ("safe", "standard", "autonomous"):
            items.append((f"Permissions: {perm}", "permission level", say(f"permissions {perm}")))
        for n in self.rt.router.configs():
            items.append((f"Use {n} for coding", "pin provider for implementer", say(f"use {n} for coding")))
            items.append((f"Use {n} for planning", "pin provider for planner", say(f"use {n} for planning")))
        for st in self.pstatus:
            for k, lbl in ACTIONS:
                items.append((f"{st.label}: {lbl}", "provider setup", (lambda k=k, n=st.name: self.run_worker(self.provider_action(k, n)))))
        return items

    def _gate(self, kind: str):  # returns a coroutine factory for the palette
        async def go() -> None:
            r = await ToolBox(self.rt.project, self.rt.perms, self.rt.bus).execute(kind, {})
            self.rt.bus.emit("TESTS" if kind == "tests" else "BUILD", "PASS" if r.ok else "FAIL", f"{kind}: {r.summary}")
            self._dirty = True
        return go

    async def _preview(self) -> None:
        r = await ToolBox(self.rt.project, self.rt.perms, self.rt.bus).execute("process_manager", {"action": "start", "name": "dev"})
        self.rt.bus.emit("BROWSER" if r.ok else "ERRORS", "PREVIEW", r.summary)
        self.set_view("preview")

    def on_unmount(self) -> None:
        self.rt.agent.stop()
        for p in self.rt.project.procs.list():
            if p["status"] == "running" and p["started"] >= self.started_at:
                self.rt.project.procs.stop(p["name"])


def _has(d: dict, dotted: str) -> bool:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def run_tui(path: str = ".", local: bool = False, initial: str | None = None, permission: str | None = None) -> None:
    rt = build_runtime(path, permission=permission, mode="local_only" if local else None, seed=True)
    keys = rt.project.cfg.get("ui.keys", {}) or {}
    if "palette" in keys:
        GeniusApp.COMMAND_PALETTE_BINDING = keys["palette"]
    GeniusApp(rt, initial).run()
