"""Assemble the object graph (project, router, permissions, bus, agent) used by CLI, TUI and headless mode."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import Agent
from .events import EventBus
from .permissions import AskFn, Permissions
from .project import Project, global_store
from .router import Router


@dataclass
class Runtime:
    project: Project
    router: Router
    perms: Permissions
    bus: EventBus
    agent: Agent
    gstore: Any

    def close(self) -> None:
        self.project.procs.stop_all() if False else None
        self.gstore.close()


def build_runtime(path: str | Path = ".", ask: AskFn | None = None, permission: str | None = None, transport=None,
                  mode: str | None = None, seed: bool = False) -> Runtime:
    project = Project.open(path)
    if seed:
        from .config import seed_default
        if seed_default(project.cfg):
            project.cfg.reload()
    gstore = global_store()
    bus = EventBus(project.store)
    perm_mode = permission or project.cfg.permission
    perms = Permissions(perm_mode, project.root, project.cfg.get("protect.paths", []), ask)
    router = Router(project.cfg, project.store, gstore, bus, project.name, transport)
    if mode:
        router.mode_override = mode
    agent = Agent(project, router, perms, bus)
    return Runtime(project, router, perms, bus, agent, gstore)


def recover_crashed_state(project: Project, bus: EventBus | None = None) -> dict[str, int]:
    """After an unclean exit: mark dangling tool calls / sessions, reconcile process table. Never assume they finished."""
    s = project.store
    n_tools = s.execute("UPDATE tool_calls SET status='interrupted' WHERE status='running'").rowcount
    n_sess = s.execute("UPDATE sessions SET status='interrupted', ended=COALESCE(ended, ?) WHERE status='running'",
                       (__import__("time").time(),)).rowcount
    n_proc = len([p for p in project.procs.list() if p["status"] == "exited"])
    if bus and (n_tools or n_sess):
        bus.emit("SYSTEM", "RECOVER", f"previous session ended unexpectedly ({n_sess} session, {n_tools} interrupted tool calls) — state will be re-inspected")
    return {"sessions": n_sess, "tool_calls": n_tools, "processes": n_proc}
