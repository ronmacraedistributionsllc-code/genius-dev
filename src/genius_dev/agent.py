"""The autonomous work loop: understand → plan → checkpoint → implement → verify → debug → review → remember."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .context import ContextBuilder, Conversation
from .events import EventBus
from .permissions import Permissions
from .project import Project
from .router import AllProvidersFailed, BudgetExceeded, Router
from .runner import truncate
from .tools import ToolBox, ToolResult

MAX_STEPS = 24
MAX_ACTIONS = 6
MAX_ROUNDS = 3

PROTOCOL = """Reply with ONE JSON object and nothing else:
{"summary": "<one short sentence: what you are doing now>",
 "actions": [{"tool": "<tool name>", "args": {...}}],
 "done": false, "final": "<only when done: what you changed and why>"}
Rules: max 6 actions per reply; you will receive their results next. Read a file before editing it. Prefer patch_file for edits.
Use `tests` to verify — never claim success without running tests/build. Set done=true only after verification passes or when blocked.
Never touch secrets. Never run destructive commands. Keep changes minimal and focused on the goal."""

IMPL_SYSTEM = "[[role:{role}]]\nYou are Genius Dev's {title}, an autonomous software engineer working inside a local project.\n\nTOOLS:\n{tools}\n\n" + PROTOCOL
PLANNER_SYSTEM = """[[role:planner]]
You are Genius Dev's planner. Convert the user's goal into explicit, verifiable requirements and a task list.
Reply as JSON: {"requirements":[{"description":"","verify":"tests|build|browser|cmd:<shell command>|manual","files":[]}],
"tasks":[{"title":"","reqs":[0],"children":["subtask", ...]}]}
Each requirement must be independently checkable. Prefer `cmd:` or `tests`. Use `manual` only when no automated check exists."""
REVIEW_SYSTEM = """[[role:final_reviewer]]
You review a code diff for logic errors, security, async/race issues, error handling, duplicated code and API/UX inconsistencies.
Reply as JSON: {"verdict":"pass|warn|fail","findings":[{"severity":"high|medium|low","file":"","issue":"","fix":""}],"summary":""}
Only report problems you can point to in the diff."""


class StopRun(Exception):
    pass


def extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                esc = (c == "\\" and not esc)
                if c == '"' and not esc:
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start: i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


@dataclass
class Plan:
    goal: str
    requirements: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    files_likely: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    goal: str
    success: bool = False
    status: str = "incomplete"          # complete | incomplete | blocked | stopped | failed
    built: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    test_results: str = ""
    visual_qa: str = "not applicable"
    files_changed: list[str] = field(default_factory=list)
    checkpoint: str = ""
    limitations: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    how_to_run: str = ""
    next_steps: list[str] = field(default_factory=list)
    review: str = ""
    summary: str = ""
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    def text(self) -> str:
        def sec(title: str, items: list[str] | str) -> str:
            if not items:
                return ""
            body = items if isinstance(items, str) else "\n".join(f"  · {i}" for i in items)
            return f"{title}\n{body if isinstance(items, list) else '  ' + body}\n\n"
        return (f"{'DONE' if self.success else self.status.upper()} — {self.goal}\n\n" + sec("BUILT", self.built) + sec("FIXED", self.fixed)
                + sec("VERIFIED", self.verified) + sec("TEST RESULTS", self.test_results) + sec("VISUAL QA", self.visual_qa)
                + sec("FILES CHANGED", self.files_changed) + sec("CHECKPOINT", self.checkpoint) + sec("REVIEW", self.review)
                + sec("KNOWN LIMITATIONS", self.limitations) + sec("EXTERNAL BLOCKERS", self.blockers)
                + sec("HOW TO RUN", self.how_to_run) + sec("NEXT OPTIONAL IMPROVEMENTS", self.next_steps)).rstrip()


class Agent:
    def __init__(self, project: Project, router: Router, perms: Permissions, bus: EventBus):
        self.p, self.router, self.perms, self.bus = project, router, perms, bus
        self.session_id: int | None = None
        self.tools: ToolBox | None = None
        self.state: dict[str, Any] = {"status": "idle", "op": "", "task": "", "started": 0.0, "step": 0, "context_pct": 0.0}
        self._stop = False
        self._paused = asyncio.Event()
        self._paused.set()
        self.actions_seen: Counter = Counter()
        self.edits_seen: list[str] = []
        self.stuck_events = 0
        self.tokens_in_context = 0
        self.last_gates: dict[str, ToolResult] = {}
        self._vr: Any = None                  # latest visual QA report (browser-verified requirements share one audit per verify pass)

    # ---- human control ----------------------------------------------------------------
    def stop(self) -> None:
        self._stop = True
        self._paused.set()

    def pause(self) -> None:
        self._paused.clear()
        self.state["status"] = "paused"

    def resume(self) -> None:
        self._paused.set()
        if self.state["status"] == "paused":
            self.state["status"] = "running"

    async def _gate(self) -> None:
        await self._paused.wait()
        if self._stop:
            raise StopRun()

    def _op(self, text: str) -> None:
        self.state["op"] = text

    # ---- planning ---------------------------------------------------------------------
    async def plan(self, goal: str, persist: bool = False) -> Plan:
        self._op("Planning")
        await asyncio.to_thread(self.p.index.refresh)
        pack = ContextBuilder(self.p, 16_000).build(goal)
        comp = await self.router.complete("planner", [{"role": "user", "content": f"{pack.text}\n\nGOAL: {goal}"}], PLANNER_SYSTEM, json_mode=True)
        data = extract_json(comp.text) or {}
        reqs = [r for r in data.get("requirements", []) if isinstance(r, dict) and r.get("description")]
        if not reqs:
            reqs = [{"description": goal, "verify": "tests" if self.p.info.test_cmd else "manual", "files": []}]
        plan = Plan(goal, reqs, data.get("tasks") or [{"title": goal, "reqs": [0]}])
        plan.files_likely = [f for f, _ in self.p.index.relevant_files(goal, 6)]
        plan.tests = [r["verify"] for r in reqs if r.get("verify")]
        if any(w in goal.lower() for w in ("auth", "login", "password", "payment", "delete", "migration", "database")):
            plan.risks.append("touches sensitive area — review the diff before shipping")
        if not self.p.info.test_cmd:
            plan.risks.append("no test command detected — verification will rely on build/manual checks")
        if persist:
            self._persist_plan(plan)
        return plan

    def _persist_plan(self, plan: Plan) -> list[str]:
        existing = {r["description"].strip().lower(): r["id"] for r in self.p.reqs.all()}
        ids: list[str] = []
        for r in plan.requirements:
            key = r["description"].strip().lower()
            if key in existing:
                ids.append(existing[key])
                if (self.p.reqs.get(existing[key]) or {}).get("status") in ("PASS", "FAIL"):
                    self.p.reqs.set_status(existing[key], "NOT_STARTED", "re-opened by new run")
                continue
            ids.append(self.p.reqs.add(r["description"], "user request", r.get("verify", ""), r.get("files", [])))
        self.p.tasks.clear()
        for t in plan.tasks:
            title = t if isinstance(t, str) else t.get("title", "")
            if not title:
                continue
            refs = [ids[i] for i in (t.get("reqs", []) if isinstance(t, dict) else []) if isinstance(i, int) and i < len(ids)]
            tid = self.p.tasks.add(title, None, refs)
            for c in (t.get("children", []) if isinstance(t, dict) else []):
                self.p.tasks.add(c if isinstance(c, str) else c.get("title", ""), tid, refs)
        self.p.sync_memory()
        self.bus.emit("AGENT", "PLAN", f"{len(ids)} requirements, {len(plan.tasks)} tasks")
        return ids

    # ---- main run ---------------------------------------------------------------------
    async def run(self, goal: str = "", plan: Plan | None = None, ui_checks: bool | None = None) -> RunReport:
        t0 = time.time()
        goal = goal.strip()
        resuming = not goal
        if resuming:
            open_reqs = [r for r in self.p.reqs.all() if r["status"] not in ("PASS", "WAIVED", "BLOCKED")]
            goal = "Continue: " + ("; ".join(r["description"] for r in open_reqs[:3]) if open_reqs else "verify project state")
        report = RunReport(goal)
        self._stop = False
        self.state.update(status="running" if self._paused.is_set() else "paused", started=t0, step=0, task=goal, op="Understanding request")
        self.session_id = self.p.start_session(goal)
        self.bus.session_id = self.router.session_id = self.session_id
        self.tools = ToolBox(self.p, self.perms, self.bus, self.session_id)
        self.p.set_current_task(goal)
        self.bus.emit("AGENT", "GOAL", goal)
        try:
            await self._gate()
            self._op("Inspecting project")
            stats = await asyncio.to_thread(self.p.index.refresh)
            self.bus.emit("AGENT", "INSPECT", f"{stats['files']} files indexed ({stats['changed']} changed)")
            if resuming and any(r["status"] not in ("PASS", "WAIVED") for r in self.p.reqs.all()):
                pass                                               # reuse persisted requirements
            else:
                plan = plan or await self.plan(goal)
                self._persist_plan(plan)
            await self._gate()
            self._op("Creating checkpoint")
            cp = await asyncio.to_thread(self.p.checkpoints.create, f"before: {goal[:80]}", "auto")
            report.checkpoint = f"{cp['id']} ({cp['commit_hash'][:7]}) — `genius undo` reverts this run"
            self.bus.emit("AGENT", "CHECKPOINT", cp["id"])
            for r in self.p.reqs.all():
                if r["status"] in ("NOT_STARTED", "FAIL"):
                    self.p.reqs.set_status(r["id"], "IN_PROGRESS", r["result"])

            failure_text, prev_sig = "", ""
            verdicts: dict[str, str] = {}
            for rnd in range(MAX_ROUNDS):
                await self._gate()
                role = "implementer" if rnd == 0 else "debugger"
                self._sync_tasks()
                await self._implement(goal, role, failure_text, rnd)
                verdicts, failure_text = await self._verify()
                failing = [k for k, v in verdicts.items() if v == "FAIL"]
                if not failing:
                    break
                sig = hashlib.sha1(failure_text[:1500].encode()).hexdigest()
                if sig == prev_sig:
                    self.bus.emit("AGENT", "STUCK", "same failure after a repair round — stopping loop")
                    report.limitations.append("Repair loop stopped: identical failure persisted across rounds")
                    break
                prev_sig = sig
                self.bus.emit("AGENT", "RETRY", f"{len(failing)} failing — starting repair round {rnd + 2}")

            # visual QA for UI changes
            await self._gate()
            frontend_touched = any(f.endswith((".html", ".css", ".scss", ".tsx", ".jsx", ".vue", ".svelte")) for f in self.tools.changed)
            if (ui_checks if ui_checks is not None else (self.p.info.is_web and frontend_touched)):
                self._op("Visual QA")
                from .visual import visual_qa
                vr = self._vr or await visual_qa(self.p, self.tools, self.router, self.bus)
                report.visual_qa = vr.text()
                if vr.ran and not vr.ok:
                    self.bus.emit("BROWSER", "UI", f"{len(vr.failures)} visual failures — repairing")
                    await self._implement(goal + "\nFix these UI problems:\n" + "\n".join(vr.failures[:10] + vr.console_errors[:5]), "implementer", "", 1)
                    vr = await visual_qa(self.p, self.tools, self.router, self.bus)
                    report.visual_qa = vr.text()
                if not vr.ran:
                    report.limitations.append(vr.text())
            await self._final_review(report)
            self._finish(report, verdicts)
            self._sync_tasks(running=False)
        except StopRun:
            report.status = "stopped"
            report.summary = "Stopped by user; checkpoint available."
            self.bus.emit("AGENT", "STOPPED", "run stopped by user")
        except BudgetExceeded as e:
            report.status, report.summary = "blocked", f"Daily budget reached: {e}"
            report.blockers.append("API budget exhausted — raise it with `genius budget <usd>` or switch to a local model")
        except AllProvidersFailed as e:
            report.status, report.summary = "blocked", "No model provider could serve the request"
            report.blockers.append(f"providers: {e}")
            self.bus.emit("ERRORS", "MODEL", "all providers failed", detail=str(e))
        except Exception as e:  # noqa: BLE001
            report.status, report.summary = "failed", f"{type(e).__name__}: {e}"
            self.bus.emit("ERRORS", "AGENT", f"run failed: {e}")
        finally:
            for pr in self.p.procs.list():                     # servers this run started for verification must not outlive it
                if pr["status"] == "running" and pr["started"] >= t0 - 1:
                    self.p.procs.stop(pr["name"])
            report.elapsed = time.time() - t0
            report.files_changed = sorted(self.tools.changed) if self.tools else []
            try:
                final_cp = await asyncio.to_thread(self.p.checkpoints.create, f"after: {goal[:80]}", "auto")
                report.checkpoint = report.checkpoint + f"; after-state {final_cp['id']}"
            except Exception:  # noqa: BLE001
                pass
            self.p.set_current_task("_Idle._")
            try:
                await asyncio.to_thread(self.p.write_architecture)
            except Exception:  # noqa: BLE001 — memory refresh must never mask the run result
                pass
            self.p.append_mem("progress.md", f"- {time.strftime('%Y-%m-%d %H:%M')} — {goal[:100]} → {report.status.upper()} "
                                             f"({self.p.reqs.summary().counts['PASS']} PASS / {self.p.reqs.summary().counts['FAIL']} FAIL)")
            self.p.sync_memory()
            self.p.end_session(self.session_id, report.status, report.summary or report.test_results)
            self.state.update(status="idle", op="")
        return report

    # ---- implement loop --------------------------------------------------------------
    async def _implement(self, goal: str, role: str, failures: str, rnd: int) -> None:
        assert self.tools
        self._op(f"{'Implementing' if role == 'implementer' else 'Debugging'}")
        pack = await asyncio.to_thread(ContextBuilder(self.p, 20_000).build, goal, failures)
        system = IMPL_SYSTEM.replace("{role}", role).replace("{title}", role.upper()).replace("{tools}", self.tools.descriptions())
        conv = Conversation()
        first = f"{pack.text}\n\nGOAL: {goal}\n" + (f"\nThe previous attempt FAILED verification. Diagnose from evidence, then fix:\n{truncate(failures, 4000)}\n" if failures else "")
        conv.add("user", first)
        self.actions_seen.clear()
        bad_json = 0
        for step in range(MAX_STEPS):
            await self._gate()
            self.state["step"] = step + 1
            comp = await self.router.complete(role, conv.assemble(), system, critical=True, json_mode=True, max_tokens=4096)
            conv.add("assistant", comp.text)
            self._update_context(conv, system)
            data = extract_json(comp.text)
            if data is None:
                bad_json += 1
                if bad_json >= 3:
                    self.bus.emit("ERRORS", "AGENT", "model kept returning invalid JSON")
                    return
                conv.add("user", "Your reply was not a single valid JSON object. Reply again following the protocol exactly.")
                continue
            summary = str(data.get("summary", ""))[:160]
            if summary:
                self._op(summary)
                self.bus.emit("AGENT", "STEP", summary)
            actions = [a for a in (data.get("actions") or []) if isinstance(a, dict)][:MAX_ACTIONS]
            if data.get("done") and not actions:
                if data.get("final"):
                    self.bus.emit("AGENT", "NOTE", str(data["final"])[:200])
                    self.p.log_decision(str(data["final"])[:160], source=role)
                return
            results: list[str] = []
            for i, a in enumerate(actions, 1):
                await self._gate()
                name, args = a.get("tool", ""), a.get("args") or {}
                sig = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
                self.actions_seen[sig] += 1
                stuck = self._stuck(name, args, sig)
                if stuck:
                    self.stuck_events += 1
                    self.bus.emit("AGENT", "STUCK", stuck)
                    if self.stuck_events >= 2:
                        self.bus.emit("AGENT", "STUCK", "still looping after reassessment — ending this round")
                        return
                    conv.add("user", f"STOP: {stuck}. Reassess and take a different approach; do not repeat that action.")
                    if role != "debugger":                       # escalate: the debugger role routes to the strongest model
                        role = "debugger"
                        system = IMPL_SYSTEM.replace("{role}", role).replace("{title}", "DEBUGGER (reassessing a stuck loop)").replace("{tools}", self.tools.descriptions())
                        self.bus.emit("AGENT", "ESCALATE", "handing the stuck task to the stronger model")
                    results = []
                    break
                res = await self.tools.execute(name, args)
                results.append(f"[{i}] {name} → {res.for_model(3500)}")
            if results:
                conv.add("user", "TOOL RESULTS:\n" + "\n\n".join(results))
            elif not actions:
                conv.add("user", "No actions were given and done=false. Either act or set done=true with a final summary.")
            if data.get("done"):
                if data.get("final"):
                    self.bus.emit("AGENT", "NOTE", str(data["final"])[:200])
                return
            await self._maybe_compact(conv)
        self.bus.emit("AGENT", "LIMIT", f"step limit ({MAX_STEPS}) reached")

    def _stuck(self, name: str, args: dict, sig: str) -> str:
        if self.actions_seen[sig] >= 3 and name not in ("tests", "build", "lint", "typecheck"):
            return f"`{name}` repeated 3 times with identical arguments"
        if name in ("patch_file", "write_file"):
            self.edits_seen.append(str(args.get("path")))
            recent = self.edits_seen[-8:]
            if recent.count(str(args.get("path"))) >= 6:
                return f"{args.get('path')} edited {recent.count(str(args.get('path')))} times in a short span"
        if name == "tests":
            n = self.actions_seen[sig]
            if n >= 5:
                return "tests run 5 times without changing approach"
        return ""

    def _update_context(self, conv: Conversation, system: str) -> None:
        self.tokens_in_context = conv.tokens(system)
        self.state["context_pct"] = self.router.context_usage(self.tokens_in_context)

    async def _maybe_compact(self, conv: Conversation) -> None:
        if self.state["context_pct"] < 0.70 or not conv.compactable():
            return
        old = conv.compactable()
        text = "\n".join(f"{m['role']}: {truncate(m['content'], 800)}" for m in old)
        self.bus.emit("AGENT", "COMPACT", f"context at {self.state['context_pct']:.0%} — summarising and saving state")
        try:
            comp = await self.router.complete("summarizer", [{"role": "user", "content": "Summarise progress so far in <=200 words: files changed, failures seen, next step.\n\n" + text}],
                                              "[[role:summarizer]]\nYou compress agent work logs faithfully.", critical=True)
            summary = comp.text
        except Exception:  # noqa: BLE001
            summary = "Earlier steps: " + "; ".join(truncate(m["content"], 120) for m in old if m["role"] == "assistant")[:800]
        conv.apply_compaction(summary)
        self.p.append_mem("progress.md", f"- {time.strftime('%H:%M')} compacted context: {summary[:200]}")

    # ---- task tree ---------------------------------------------------------------------
    def _sync_tasks(self, running: bool = True) -> None:
        """Derive task states from requirement evidence (tasks never claim completion on their own)."""
        reqs = {r["id"]: r["status"] for r in self.p.reqs.all()}
        all_done = bool(reqs) and all(v in ("PASS", "WAIVED") for v in reqs.values())
        tasks = self.p.tasks

        def leaf(t: dict) -> str:
            ids = t["req_ids"]
            if ids:
                st = [reqs.get(i) for i in ids]
                if all(x in ("PASS", "WAIVED") for x in st):
                    return "done"
                return "blocked" if any(x == "BLOCKED" for x in st) else "todo"
            return "done" if all_done else "todo"

        def walk(nodes: list[dict]) -> list[str]:
            out = []
            for n in nodes:
                st = ("done" if all(x == "done" for x in walk(n["children"])) else "todo") if n["children"] else leaf(n)
                if n["status"] != st and not (n["status"] == "active" and st == "todo"):
                    tasks.set_status(n["id"], st)
                out.append(st)
            return out
        walk(tasks.tree())
        if running:
            flat = tasks.flat()
            for i, (_, t) in enumerate(flat):
                t_now = next(x for _, x in tasks.flat() if x["id"] == t["id"])
                if t_now["status"] in ("todo", "active") and not t["children"]:
                    tasks.set_status(t["id"], "active")
                    break
            for _, t in tasks.flat():
                if t["children"] and t["status"] != "done":
                    tasks.set_status(t["id"], "active" if any(c["status"] == "active" for c in next(x for _, x in tasks.flat() if x["id"] == t["id"])["children"]) else "todo")

    async def verify_only(self) -> dict[str, str]:
        """Re-run every gate and every requirement's verification and update statuses from the evidence (no model, no edits)."""
        if self.tools is None:
            self.tools = ToolBox(self.p, self.perms, self.bus, self.session_id)
        verdicts, _ = await self._verify()
        self.p.sync_memory()
        return verdicts

    # ---- verification -----------------------------------------------------------------
    async def _verify(self) -> tuple[dict[str, str], str]:
        """Run gates, then set every requirement's status from evidence only."""
        assert self.tools
        self._op("Verifying")
        self._vr = None
        if self.tools.changed:
            self.p.redetect()                                  # new files may change what the project is (e.g. a first index.html → static site)
        gates: dict[str, ToolResult] = {}
        info = self.p.info
        for kind, cmd in (("lint", info.lint_cmd), ("typecheck", info.typecheck_cmd), ("tests", info.test_cmd), ("build", info.build_cmd)):
            if cmd:
                gates[kind] = await self.tools.execute(kind, {})
        self.last_gates = gates
        failure_lines = [f"{k.upper()} FAILED: {truncate(g.output, 2500)}" for k, g in gates.items() if not g.ok]
        verdicts: dict[str, str] = {}
        for r in self.p.reqs.all():
            if r["status"] in ("WAIVED", "BLOCKED"):
                continue
            v = (r["verify"] or "").replace("{py}", shlex.quote(info.python or "python3"))
            status, result = "IN_PROGRESS", "no automated verification available"
            if v in ("tests", "test"):
                g = gates.get("tests")
                if g is None:
                    status, result = "IN_PROGRESS", "no test command detected"
                elif g.ok and g.data.get("total", 0) == 0:
                    status, result = "IN_PROGRESS", "test command ran but found no tests"
                else:
                    status, result = ("PASS" if g.ok else "FAIL"), g.summary
            elif v == "build":
                g = gates.get("build")
                status, result = (("PASS" if g.ok else "FAIL"), g.summary) if g else ("IN_PROGRESS", "no build command detected")
            elif v.startswith("cmd:"):
                res = await self.tools.execute("terminal", {"command": v[4:].strip()})
                if res.denied:
                    status, result = "IN_PROGRESS", "verification command not permitted"
                else:
                    tail = (res.output.strip().splitlines() or [""])[-1][:60]
                    status, result = ("PASS" if res.ok else "FAIL"), f"exit {res.data.get('code', 0)} — {tail}"
                    if not res.ok:
                        failure_lines.append(f"{r['id']} verify `{v[4:].strip()}` failed:\n{truncate(res.output, 1500)}")
            elif v.startswith("browser"):
                if self._vr is None:
                    from .visual import visual_qa
                    self._op("Visual QA")
                    self._vr = await visual_qa(self.p, self.tools, self.router, self.bus)
                vr = self._vr
                if not vr.ran:
                    status, result = "IN_PROGRESS", f"cannot verify in a browser: {vr.reason}"
                elif vr.ok:
                    status, result = "PASS", f"audit clean on {vr.url} (desktop + tablet + mobile)"
                else:
                    status, result = "FAIL", f"{len(vr.failures)} UI failure(s), {len(vr.console_errors)} console error(s)"
                    failure_lines.append("BROWSER AUDIT FAILED:\n" + "\n".join(vr.failures[:12] + vr.console_errors[:5]))
            verdicts[r["id"]] = status
            self.p.reqs.set_status(r["id"], status, result)
            if status == "PASS":
                self.p.reqs.set_files(r["id"], sorted(self.tools.changed) or r["files"])
            self.bus.emit("AGENT", "VERIFY", f"{r['id']} {status}: {r['description'][:60]}") if status != r["status"] else None
        self.p.sync_memory()
        self._sync_tasks()
        gate_fail = any(not g.ok for g in gates.values())
        if gate_fail and not any(v == "FAIL" for v in verdicts.values()):
            verdicts["_gates"] = "FAIL"                     # a red gate keeps the loop going even if no req maps to it
        return verdicts, "\n\n".join(failure_lines)

    # ---- review & report ---------------------------------------------------------------
    async def _final_review(self, report: RunReport) -> None:
        assert self.tools
        changed = sorted(self.tools.changed)
        diff = await asyncio.to_thread(self.p.git.diff) if self.p.git.is_repo else ""
        lines = diff.count("\n")
        if not changed or (len(changed) < 3 and lines < 40):
            return                                        # tiny change: don't burn model calls
        self._op("Final review")
        try:
            comp = await self.router.complete("final_reviewer", [{"role": "user", "content": truncate(diff, 14000, keep_tail=False)}], REVIEW_SYSTEM, json_mode=True)
        except Exception as e:  # noqa: BLE001
            report.limitations.append(f"final review skipped: {str(e)[:80]}")
            return
        data = extract_json(comp.text) or {}
        finds = data.get("findings", [])
        report.review = f"{data.get('verdict', '?')}: {data.get('summary', '')}"[:300]
        for f in finds:
            if f.get("severity") == "high":
                self.p.add_bug(f"[review] {f.get('file', '')}: {f.get('issue', '')}"[:200], f.get("fix", ""))
                report.limitations.append(f"review (high): {f.get('file', '')} — {f.get('issue', '')}"[:200])

    def _finish(self, report: RunReport, verdicts: dict[str, str]) -> None:
        sm = self.p.reqs.summary()
        c = sm.counts
        last = self.p.last_run("tests")
        report.test_results = (f"{last['passed']}/{last['total']} passing" + (f", {last['failed']} failing" if last["failed"] else "")) if last else "no tests run"
        b = self.p.last_run("build")
        if b:
            report.test_results += f" · build {'PASS' if b['ok'] else 'FAIL'}"
        report.verified = [f"{r['id']} {r['description'][:80]}" for r in self.p.reqs.all() if r["status"] == "PASS"]
        report.fixed = [r["description"] for r in self.p.reqs.all() if r["status"] == "PASS" and self.tools and self.tools.changed]
        open_ = [r for r in self.p.reqs.all() if r["status"] in ("FAIL", "IN_PROGRESS", "NOT_STARTED")]
        report.blockers += [f"{r['id']} blocked: {r['result']}" for r in self.p.reqs.all() if r["status"] == "BLOCKED"]
        if open_:
            report.limitations += [f"{r['id']} {r['status']}: {r['description'][:70]} — {r['result'][:70]}" for r in open_]
        report.success = sm.complete and not any(v == "FAIL" for v in verdicts.values())
        report.status = "complete" if report.success else "incomplete"
        report.summary = f"{c['PASS']} PASS, {c['FAIL']} FAIL, {c['BLOCKED']} BLOCKED — {sm.resolved_pct:.0f}% resolved"
        info = self.p.info
        report.how_to_run = self.p.pretty(info.dev_cmd or info.test_cmd or "see README")
