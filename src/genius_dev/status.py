"""Status snapshot & resume summary shared by CLI, TUI and headless output."""
from __future__ import annotations

import time
from typing import Any

from .runtime import Runtime, recover_crashed_state


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 90 else f"{s // 60}m ago" if s < 5400 else f"{s // 3600}h ago" if s < 172800 else f"{s // 86400}d ago"


_BRANCH: dict[str, tuple[float, str | None]] = {}


def _branch(p) -> str | None:
    hit = _BRANCH.get(str(p.root))
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    b = p.git.branch() if p.git.is_repo else None
    _BRANCH[str(p.root)] = (time.time(), b)
    return b


def snapshot(rt: Runtime) -> dict[str, Any]:
    p, r = rt.project, rt.router
    sm = p.reqs.summary()
    route = r.route("implementer")
    tests, build = p.last_run("tests"), p.last_run("build")
    spent, limit = r.budget()
    cps = [c for c in p.checkpoints.list() if c["kind"] != "pre-restore"]
    cur = p.tasks.current()
    return {
        "project": p.name, "root": str(p.root), "kind": p.info.kind, "stack": ", ".join(p.info.frameworks or p.info.languages) or "—",
        "branch": _branch(p),
        "permission": rt.perms.mode, "routing_mode": r.mode,
        "model": route.primary, "fallback": route.chain[1] if len(route.chain) > 1 else None, "route_reason": route.reason,
        "tests": {"passed": tests["passed"], "failed": tests["failed"], "total": tests["total"], "ok": bool(tests["ok"]), "ago": ago(tests["ts"])} if tests else None,
        "build": None if not build else bool(build["ok"]),
        "requirements": {"total": sm.total, **sm.counts, "resolved_pct": round(sm.resolved_pct, 1), "complete": sm.complete},
        "progress": round(p.progress_pct(), 1),
        "current_task": cur["title"] if cur else None,
        "agent": dict(rt.agent.state),
        "context_pct": round(rt.agent.state.get("context_pct", 0.0) * 100),
        "cost_today": round(spent, 4), "budget": limit, "budget_state": r.budget_state(),
        "last_session": p.last_session(), "last_checkpoint": cps[0] if cps else None,
        "blockers": [f"{q['id']}: {q['result'] or q['description']}" for q in p.reqs.all() if q["status"] == "BLOCKED"],
        "open_bugs": [b["title"] for b in p.store.query("SELECT title FROM bugs WHERE status='open'")],
        "index": p.index.stats(),
    }


def resume_view(rt: Runtime) -> dict[str, Any]:
    """Reconstruct the previous session. Recovery runs first: nothing interrupted is assumed complete."""
    rec = recover_crashed_state(rt.project, rt.bus)
    s = snapshot(rt)
    last = s["last_session"]
    fails = [f for f in rt.project.reqs.all() if f["status"] == "FAIL"]
    procs = [f"{x['name']} (pid {x['pid']}{', ' + x['url'] if x['url'] else ''})" for x in rt.project.procs.list() if x["status"] == "running"]
    return {**s, "recovery": rec, "processes": procs,
            "failing_reqs": [f"{f['id']} {f['description'][:70]}" for f in fails],
            "open_reqs": [f"{f['id']} {f['description'][:70]}" for f in rt.project.reqs.all() if f["status"] in ("NOT_STARTED", "IN_PROGRESS")],
            "last_session_text": f"{last['goal'][:90]} — {last['status']} ({ago(last['ended'] or last['started'])})" if last else None}


def resume_text(v: dict[str, Any]) -> str:
    t = v["tests"]
    reqs = v["requirements"]
    lines = ["PROJECT", f"  {v['project']}  ·  {v['kind']}  ·  {v['stack']}", "",
             "LAST SESSION", f"  {v['last_session_text'] or 'none recorded'}", "",
             "LAST CHECKPOINT", f"  {v['last_checkpoint']['id']} — {v['last_checkpoint']['task'][:60]} ({ago(v['last_checkpoint']['ts'])})" if v["last_checkpoint"] else "  none", "",
             "CURRENT TASK", f"  {v['current_task'] or 'idle'}", "",
             "COMPLETION", f"  {v['progress']:.0f}%   {reqs['PASS']} pass · {reqs['FAIL']} fail · {reqs['BLOCKED']} blocked · {reqs['NOT_STARTED'] + reqs['IN_PROGRESS']} open", "",
             "PASSING TESTS", f"  {t['passed']}/{t['total']} (last run {t['ago']})" if t else "  not run yet", "",
             "FAILING", *(["  " + f for f in v["failing_reqs"][:6]] or ["  none"]), "",
             "KNOWN BLOCKERS", *(["  " + b for b in v["blockers"][:5]] or ["  none"])]
    if v.get("processes"):
        lines += ["", "STILL RUNNING", *["  " + x for x in v["processes"]], "  (from a previous session — `genius stop` to clean up)"]
    if v["recovery"]["sessions"] or v["recovery"]["tool_calls"]:
        lines += ["", "RECOVERY", f"  previous session ended unexpectedly — {v['recovery']['tool_calls']} interrupted tool call(s); state will be re-inspected"]
    return "\n".join(lines)
