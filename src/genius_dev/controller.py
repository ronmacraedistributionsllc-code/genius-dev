"""Executes interpreted user intent against the runtime. Shared by the TUI and the headless REPL."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from .audit import review_diff, run_finish, security_findings
from .debate import run_debate
from .doctor import run_doctor
from .handoff import build_handoff
from .intent import Intent, interpret
from .runtime import Runtime
from .status import resume_text, resume_view
from .tools import ToolBox


class Controller:
    def __init__(self, rt: Runtime, show: Callable[[str, str], None] | None = None, open_view: Callable[[str], None] | None = None):
        self.rt = rt
        self.show = show or (lambda title, body: None)          # show(title, body): present a long result
        self.open_view = open_view or (lambda v: None)
        self.task: asyncio.Task | None = None
        self.last_report: Any = None
        self.on_doctor: Any = None

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def say(self, msg: str, label: str = "SAY") -> None:
        self.rt.bus.emit("AGENT", label, msg)

    async def handle(self, text: str) -> None:
        it = interpret(text, agent_active=self.busy)
        await self.dispatch(it)

    def _spawn(self, coro) -> None:
        if self.busy:
            self.say("Already working — type `stop` first, or wait for the current run to finish.")
            coro.close()
            return
        self.task = asyncio.create_task(coro)

    async def dispatch(self, it: Intent) -> None:
        rt, a = self.rt, self.rt.agent
        k = it.kind
        if k == "empty":
            return
        if k == "stop":
            a.stop()
            self.say("Stopping after the current step…")
        elif k == "pause":
            a.pause()
            self.say("Paused. Type `resume` to continue.")
        elif k == "resume_run":
            a.resume()
            self.say("Resumed.")
        elif k == "model":
            self._model(it.args)
        elif k == "remember":
            rt.project.remember(it.args["text"])
            self.say("Noted for this project: " + it.args["text"][:80])
        elif k == "protect":
            self._protect(it.args["target"])
        elif k == "restore":
            self._restore(it.args.get("id"))
        elif k == "skip":
            self._skip(it.args.get("id"))
        elif k == "diff":
            self.open_view("diff")
        elif k == "view":
            v = it.args["view"]
            self.open_view("cost" if v == "costs" else v)
        elif k == "resume":
            self._spawn(self.resume())
        elif k == "finish":
            self._spawn(self.finish())
        elif k == "plan":
            self._spawn(self.plan(it.args["goal"]))
        elif k == "command":
            await self._command(it.args)
        elif k == "goal":
            self._spawn(self.run_goal(it.args["goal"]))

    # ---- actions ----------------------------------------------------------------------
    async def run_goal(self, goal: str):
        rep = await self.rt.agent.run(goal)
        self.last_report = rep
        self.show("Result", rep.text())
        return rep

    async def resume(self):
        v = resume_view(self.rt)
        self.show("Resume", resume_text(v))
        open_n = v["requirements"]["FAIL"] + v["requirements"]["NOT_STARTED"] + v["requirements"]["IN_PROGRESS"]
        if not open_n:
            self.say("Nothing outstanding — all requirements are resolved. Give me something new to do.")
            return None
        self.say(f"Re-inspecting state, then continuing {open_n} open requirement(s)…")
        return await self.run_goal("")

    async def plan(self, goal: str):
        plan = await self.rt.agent.plan(goal)
        body = ["PLAN (nothing modified)", f"Goal: {goal}", "", "REQUIREMENTS"] + [f"  · {r['description']}   [{r.get('verify', '?')}]" for r in plan.requirements]
        body += ["", "TASKS"] + [f"  · {t if isinstance(t, str) else t.get('title')}" for t in plan.tasks]
        body += ["", "FILES LIKELY AFFECTED"] + [f"  · {f}" for f in plan.files_likely] + ["", "RISKS"] + [f"  · {r}" for r in plan.risks or ["none identified"]]
        body += ["", "TESTS"] + [f"  · {t}" for t in plan.tests]
        body += ["", f"To execute: `genius run \"{goal}\"` or type the goal again without `plan`."]
        self.show("Plan", "\n".join(body))
        return plan

    async def finish(self):
        rt = self.rt
        tools = ToolBox(rt.project, rt.perms, rt.bus)
        rep = await run_finish(rt.project, tools, rt.router, rt.bus, rt.agent)
        self.show("Finish audit", rep.text())
        return rep

    def _model(self, a: dict[str, Any]) -> None:
        r = self.rt.router
        names = {n.lower(): n for n in r.configs()}
        want = a["provider"].lower()
        hit = names.get(want) or next((n for l, n in names.items() if want in l or want in (r.configs()[n].model or "").lower()), None)
        if not hit:
            self.say(f"No configured provider matches '{a['provider']}'. Configured: {', '.join(names.values()) or 'none'}")
            return
        roles = [a["role"]] if a["role"] else ["implementer", "planner", "debugger", "reviewer", "final_reviewer", "summarizer"]
        for role in roles:
            r.pins[role] = hit
        self.rt.project.log_decision(f"routing: {a['role'] or 'all roles'} → {hit}", "chosen by the user", "user")
        self.say(f"Routing {a['role'] or 'all roles'} → {hit}")

    def _protect(self, target: str) -> None:
        aliases = {"frontend": ["frontend", "client", "web", "src/components", "public"], "backend": ["backend", "server", "api"], "tests": ["tests", "test"]}
        paths = aliases.get(target, [target])
        existing = [p for p in paths if (self.rt.project.root / p).exists()] or [target]
        for p in existing:
            if p not in self.rt.perms.protected:
                self.rt.perms.protected.append(p)
        self.rt.project.cfg.set("protect.paths", list(self.rt.perms.protected), "project")
        self.say(f"Protected: {', '.join(existing)} — the agent will not modify these without permission")

    def _restore(self, cid: str | None) -> None:
        try:
            res = self.rt.project.checkpoints.restore(cid) if cid else self.rt.project.checkpoints.undo()
            self.say(f"Restored {res['checkpoint']} — {len(res['restored'])} restored, {len(res['deleted'])} removed (undo with `restore {res['safety_checkpoint']}`)")
        except Exception as e:  # noqa: BLE001
            self.say(f"Cannot restore: {e}")

    def _skip(self, rid: str | None) -> None:
        reqs = self.rt.project.reqs
        rid = rid or next((r["id"] for r in reqs.all() if r["status"] in ("IN_PROGRESS", "FAIL", "NOT_STARTED")), None)
        if not rid:
            self.say("No open requirement to skip.")
            return
        reqs.waive(rid, "skipped by user")
        self.rt.project.sync_memory()
        self.say(f"{rid} waived by user")

    async def _command(self, a: dict[str, Any]) -> None:
        rt, cmd = self.rt, a["cmd"]
        if cmd == "budget":
            rt.project.cfg.set("general.daily_budget", a["value"])
            self.say(f"Daily budget set to ${a['value']:.2f}")
        elif cmd == "mode":
            rt.router.mode_override = None
            rt.project.cfg.set("general.routing_mode", a["value"])
            self.say(f"Routing mode → {a['value']}")
        elif cmd == "permission":
            rt.perms.mode = a["value"]
            rt.project.cfg.set("general.permission", a["value"])
            self.say(f"Permission mode → {a['value'].upper()}")
        elif cmd == "status":
            self.open_view("home")
        elif cmd == "doctor":
            rep = await run_doctor(rt.project, rt.router, True)
            if self.on_doctor:
                self.on_doctor(rep)
                return
            lines = []
            for sec, cs in rep.sections.items():
                lines += [sec] + [f"  {'✓' if c.status == 'ok' else '✕' if c.status == 'fail' else '!' if c.status == 'warn' else '·'} {c.name:22} {c.detail}" for c in cs] + [""]
            self.show("Doctor", "\n".join(lines))
        elif cmd == "handoff":
            text, path = build_handoff(rt.project)
            self.say(f"Handoff written: {path.relative_to(rt.project.root)}")
        elif cmd == "review":
            self._spawn(self._review())
        elif cmd == "security":
            fs = security_findings(rt.project)
            self.show("Security", "\n".join(f.text() for f in fs) or "No findings from static checks.")
        elif cmd == "checkpoint":
            cp = rt.project.checkpoints.create("manual checkpoint", "manual")
            self.say(f"Checkpoint {cp['id']} created")
        elif cmd == "debate":
            self._spawn(self._debate(a["text"]))
        elif cmd in ("preview", "processes"):
            self.open_view("preview")
        elif cmd == "help":
            self.show("Help", HELP)
        else:
            self.say(f"`{cmd}` is available as a CLI command: genius {cmd}")

    async def _review(self):
        d = await review_diff(self.rt.project, self.rt.router)
        body = [f"REVIEW — {d.get('verdict', '?').upper()}", d.get("summary", ""), ""] + [f"[{f.get('severity')}] {f.get('file')}: {f.get('issue')}  → {f.get('fix')}" for f in d.get("findings", [])]
        self.show("Review", "\n".join(body))

    async def _debate(self, issue: str):
        r = await run_debate(self.rt.project, self.rt.router, self.rt.perms, self.rt.bus, issue)
        self.show("Debate", r.text())


HELP = """Talk naturally — for example:
  Build me a delivery management app.
  Fix merchant signup and make sure login works.
  Continue where you stopped yesterday.
  Find everything preventing this app from being production ready.

Control while working:  stop · pause · resume · use <model> for coding · change model to <name>
                        don't touch the frontend · restore last checkpoint · skip this requirement · show me what changed

Other:  plan: <goal> · debate: <question> · budget 5 · mode economy · permissions safe · doctor · review · security · handoff

Keys:   ctrl+k palette · ctrl+p tasks · ctrl+l logs · ctrl+t tests · ctrl+d diff · ctrl+v preview · ctrl+m models · ctrl+r resume · esc home"""
