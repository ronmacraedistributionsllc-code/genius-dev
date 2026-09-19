"""Requirements and task tracking. PASS is only ever set from verification evidence."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .db import Store

STATUSES = ["NOT_STARTED", "IN_PROGRESS", "PASS", "FAIL", "BLOCKED", "WAIVED"]
DONE_STATES = {"PASS", "BLOCKED", "WAIVED"}
TASK_STATES = ["todo", "active", "done", "blocked", "skipped"]


@dataclass
class Summary:
    total: int
    counts: dict[str, int]

    @property
    def resolved_pct(self) -> float:
        """(PASS + BLOCKED + WAIVED) / total — matches the 'definition of done' rule."""
        if not self.total:
            return 0.0
        return 100.0 * sum(self.counts.get(s, 0) for s in DONE_STATES) / self.total

    @property
    def pass_pct(self) -> float:
        return 100.0 * self.counts.get("PASS", 0) / self.total if self.total else 0.0

    @property
    def complete(self) -> bool:
        return self.total > 0 and all(self.counts.get(s, 0) == 0 for s in ("NOT_STARTED", "IN_PROGRESS", "FAIL"))


class Requirements:
    def __init__(self, store: Store):
        self.s = store

    def add(self, description: str, source: str = "user", verify: str = "", files: list[str] | None = None) -> str:
        n = self.s.scalar("SELECT COUNT(*) FROM requirements") + 1
        rid = f"REQ-{n:03d}"
        while self.s.one("SELECT 1 FROM requirements WHERE id=?", (rid,)):
            n += 1
            rid = f"REQ-{n:03d}"
        self.s.execute("INSERT INTO requirements(id,description,source,verify,files,updated) VALUES(?,?,?,?,?,?)",
                       (rid, description, source, verify, json.dumps(files or []), time.time()))
        return rid

    def all(self) -> list[dict[str, Any]]:
        return [dict(r) | {"files": json.loads(r["files"] or "[]")} for r in self.s.query("SELECT * FROM requirements ORDER BY id")]

    def get(self, rid: str) -> dict[str, Any] | None:
        r = self.s.one("SELECT * FROM requirements WHERE id=?", (rid,))
        return dict(r) | {"files": json.loads(r["files"] or "[]")} if r else None

    def set_status(self, rid: str, status: str, result: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(status)
        self.s.execute("UPDATE requirements SET status=?, result=?, updated=? WHERE id=?", (status, result[:2000], time.time(), rid))

    def set_files(self, rid: str, files: list[str]) -> None:
        self.s.execute("UPDATE requirements SET files=? WHERE id=?", (json.dumps(sorted(set(files))), rid))

    def waive(self, rid: str, why: str = "waived by user") -> None:
        self.set_status(rid, "WAIVED", why)

    def summary(self) -> Summary:
        counts = {s: 0 for s in STATUSES}
        for r in self.s.query("SELECT status, COUNT(*) c FROM requirements GROUP BY status"):
            counts[r["status"]] = r["c"]
        return Summary(sum(counts.values()), counts)

    def render_md(self) -> str:
        sm = self.summary()
        lines = ["# Requirements", "",
                 f"{sm.counts['PASS']} PASS · {sm.counts['FAIL']} FAIL · {sm.counts['BLOCKED']} BLOCKED · "
                 f"{sm.counts['WAIVED']} WAIVED · {sm.counts['NOT_STARTED'] + sm.counts['IN_PROGRESS']} open — "
                 f"{sm.resolved_pct:.1f}% resolved", "",
                 "| ID | Status | Requirement | Source | Verification | Files | Result |", "|---|---|---|---|---|---|---|"]
        for r in self.all():
            esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r['id']} | {r['status']} | {esc(r['description'])} | {esc(r['source'])} | {esc(r['verify'] or '—')} | "
                         f"{esc(', '.join(r['files']) or '—')} | {esc((r['result'] or '—')[:80])} |")
        return "\n".join(lines) + "\n"


class Tasks:
    def __init__(self, store: Store):
        self.s = store

    def add(self, title: str, parent_id: int | None = None, req_ids: list[str] | None = None) -> int:
        pos = self.s.scalar("SELECT COALESCE(MAX(position),0)+1 FROM tasks WHERE parent_id IS ?", (parent_id,), 1)
        now = time.time()
        return self.s.execute("INSERT INTO tasks(parent_id,title,req_ids,position,created,updated) VALUES(?,?,?,?,?,?)",
                              (parent_id, title, json.dumps(req_ids or []), pos, now, now)).lastrowid or 0

    def set_status(self, tid: int, status: str) -> None:
        self.s.execute("UPDATE tasks SET status=?, updated=? WHERE id=?", (status, time.time(), tid))

    def tree(self) -> list[dict[str, Any]]:
        rows = [dict(r) | {"req_ids": json.loads(r["req_ids"] or "[]")} for r in self.s.query("SELECT * FROM tasks ORDER BY position, id")]
        by_parent: dict[Any, list[dict]] = {}
        for r in rows:
            by_parent.setdefault(r["parent_id"], []).append(r)
        def build(pid):
            out = []
            for r in by_parent.get(pid, []):
                r["children"] = build(r["id"])
                out.append(r)
            return out
        return build(None)

    def flat(self) -> list[tuple[int, dict[str, Any]]]:
        out: list[tuple[int, dict]] = []
        def walk(nodes, depth):
            for n in nodes:
                out.append((depth, n))
                walk(n["children"], depth + 1)
        walk(self.tree(), 0)
        return out

    def current(self) -> dict[str, Any] | None:
        for _, t in self.flat():
            if t["status"] == "active":
                return t
        for _, t in self.flat():
            if t["status"] == "todo":
                return t
        return None

    def progress(self) -> float:
        rows = [t for _, t in self.flat() if not t["children"]]
        if not rows:
            return 0.0
        done = sum(1 for t in rows if t["status"] in ("done", "skipped"))
        return 100.0 * done / len(rows)

    def clear(self) -> None:
        self.s.execute("DELETE FROM tasks")
