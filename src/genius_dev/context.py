"""Context engine: pick only what matters for the task, track usage, compact when full."""
from __future__ import annotations

from dataclasses import dataclass, field

from .project import Project
from .providers import estimate_tokens
from .runner import truncate
from .secrets import find_secrets, is_secret_file, redact


@dataclass
class ContextPack:
    text: str
    files: list[str] = field(default_factory=list)
    tokens: int = 0
    skipped_secret_files: list[str] = field(default_factory=list)


class ContextBuilder:
    def __init__(self, project: Project, budget_tokens: int = 24_000):
        self.p, self.budget = project, budget_tokens

    def build(self, goal: str, errors: str = "", focus_files: list[str] | None = None) -> ContextPack:
        p = self.p
        parts: list[tuple[str, str]] = []
        info = p.info
        parts.append(("PROJECT", f"{p.name} — {info.kind}; languages: {', '.join(info.languages) or '?'}; frameworks: "
                                 f"{', '.join(info.frameworks) or '-'}; test: `{p.pretty(info.test_cmd) or 'none'}`; build: `{p.pretty(info.build_cmd) or 'none'}`"))
        prefs = p.preferences()
        if prefs:
            parts.append(("PROJECT PREFERENCES (follow these)", "\n".join("- " + x for x in prefs[-12:])))
        open_reqs = [r for r in p.reqs.all() if r["status"] not in ("PASS", "WAIVED")]
        if open_reqs:
            parts.append(("OPEN REQUIREMENTS", "\n".join(f"{r['id']} [{r['status']}] {r['description']}" + (f"  → last result: {r['result'][:120]}" if r["result"] else "") for r in open_reqs[:25])))
        dec = p.store.query("SELECT title FROM decisions ORDER BY id DESC LIMIT 5")
        if dec:
            parts.append(("RECENT DECISIONS", "\n".join("- " + d["title"] for d in dec)))
        tree = "\n".join(sorted(f["path"] for f in p.index.files() if f["lines"] > 0)[:80])
        parts.append(("FILES", tree))
        if errors:
            parts.append(("RECENT ERRORS", truncate(errors, 3000)))
        diff = p.git.diff(stat=True) if p.git.is_repo else ""
        if diff.strip():
            parts.append(("GIT DIFF (stat)", diff[:1200]))
        # relevant source, ranked
        ranked = [f for f, _ in p.index.relevant_files(goal + " " + errors, 8)]
        for f in (focus_files or []) + ranked:
            if f in ranked[:0]:
                continue
        wanted: list[str] = []
        for f in list(focus_files or []) + ranked:
            if f not in wanted:
                wanted.append(f)
        for f in list(wanted):
            for t in p.index.related_tests(f)[:2]:
                if t not in wanted:
                    wanted.append(t)
        used = sum(estimate_tokens(t) for _, t in parts)
        included, skipped = [], []
        for f in wanted:
            if is_secret_file(f):
                skipped.append(f)
                continue
            try:
                body = (p.root / f).read_text(errors="replace")
            except OSError:
                continue
            if find_secrets(body):
                body = redact(body)
            cost = estimate_tokens(body)
            if used + cost > self.budget:
                body = truncate(body, max(400, (self.budget - used) * 4), keep_tail=False)
                cost = estimate_tokens(body)
                if used + cost > self.budget:
                    break
            parts.append((f"FILE {f}", body))
            included.append(f)
            used += cost
        text = "\n\n".join(f"## {h}\n{b}" for h, b in parts)
        return ContextPack(text, included, estimate_tokens(text), skipped)


class Conversation:
    """Agent message history with token accounting and summarising compaction."""

    def __init__(self):
        self.messages: list[dict] = []
        self.summary = ""

    def add(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def tokens(self, system: str = "") -> int:
        return estimate_tokens(system + self.summary + "".join(m["content"] for m in self.messages))

    def assemble(self) -> list[dict]:
        if not self.summary:
            return list(self.messages)
        head = self.messages[:1]
        return head + [{"role": "user", "content": "EARLIER PROGRESS (compacted):\n" + self.summary}] + self.messages[1:]

    def compactable(self) -> list[dict]:
        return self.messages[1:-4] if len(self.messages) > 6 else []

    def apply_compaction(self, summary: str) -> None:
        keep_tail = self.messages[-4:]
        self.messages = self.messages[:1] + keep_tail
        # roles must alternate for some providers; ensure tail begins with assistant
        while len(self.messages) > 1 and self.messages[1]["role"] != "assistant":
            self.messages.pop(1)
        self.summary = (self.summary + "\n" + summary).strip()
