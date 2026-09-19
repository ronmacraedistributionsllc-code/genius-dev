"""genius handoff — a compact, redacted, self-contained package for switching agents."""
from __future__ import annotations

import time
from pathlib import Path

from .project import Project
from .runner import truncate
from .secrets import redact


def build_handoff(p: Project, cost_line: str = "") -> tuple[str, Path]:
    p.sync_memory()
    info, sm = p.info, p.reqs.summary()
    c = sm.counts
    last = p.last_session()
    tests = p.last_run("tests")
    lines = [
        f"# Handoff — {p.name}",
        f"_Generated {time.strftime('%Y-%m-%d %H:%M')} by Genius Dev. Secrets redacted; no .env or key files included._", "",
        "## Summary",
        f"- Kind: {info.kind}; languages: {', '.join(info.languages) or '?'}; frameworks: {', '.join(info.frameworks) or '-'}",
        f"- Package managers: {', '.join(info.package_managers) or '-'}; databases: {', '.join(info.databases) or '-'}",
        f"- Test: `{info.test_cmd or 'none'}` · Build: `{info.build_cmd or 'none'}` · Dev: `{info.dev_cmd or 'none'}`",
        f"- Progress: {c['PASS']} PASS / {c['FAIL']} FAIL / {c['BLOCKED']} BLOCKED / {c['WAIVED']} WAIVED / "
        f"{c['NOT_STARTED'] + c['IN_PROGRESS']} open ({sm.resolved_pct:.0f}% resolved)",
        f"- Last tests: {tests['passed']}/{tests['total']} passing" if tests else "- Last tests: not run",
        f"- Last session: {last['goal'][:100]} → {last['status']}" if last else "- No previous session", "",
        "## Current task", p.read_mem("current_task.md").replace("# Current task", "").strip() or "_Idle._", "",
        "## Project preferences", *(["- " + x for x in p.preferences()] or ["_none recorded_"]), "",
        "## Requirements", p.reqs.render_md().replace("# Requirements", "").strip(), "",
        "## Architecture", p.read_mem("architecture.md").replace("# Architecture", "").strip(), "",
        "## Recent decisions",
    ]
    dec = p.store.query("SELECT ts,title,detail FROM decisions ORDER BY id DESC LIMIT 12")
    lines += [f"- {d['title']}" + (f": {d['detail']}" if d["detail"] else "") for d in dec] or ["_none_"]
    lines += ["", "## Known bugs / blockers"]
    bugs = p.store.query("SELECT title FROM bugs WHERE status='open'")
    lines += [f"- {b['title']}" for b in bugs] or ["_none recorded_"]
    lines += [f"- BLOCKED {r['id']}: {r['result'] or r['description']}" for r in p.reqs.all() if r["status"] == "BLOCKED"]
    lines += ["", "## Remaining work"]
    lines += [f"- {r['id']} [{r['status']}] {r['description']}" for r in p.reqs.all() if r["status"] not in ("PASS", "WAIVED")] or ["_nothing open_"]
    lines += ["", "## Important files"]
    files = sorted(p.index.files(), key=lambda f: -f["mtime"])
    lines += [f"- `{f['path']}` ({f['lines']} lines)" for f in files if not f["is_test"] and f["lang"] not in ("md", "json", "lock")][:15]
    if p.git.is_repo:
        lines += ["", "## Recent changes (uncommitted vs HEAD)", "```", p.git.diff(stat=True).strip()[:1500] or "clean", "```",
                  "<details><summary>patch</summary>", "", "```diff", truncate(p.git.diff(), 8000, keep_tail=False), "```", "</details>"]
        lines += ["", "## Recent commits", *[f"- {l}" for l in p.git.log(8)]]
    if cost_line:
        lines += ["", "## Model spend", cost_line]
    lines += ["", "## Instructions for the next agent",
              "Read this file first, then `.genius/requirements.md`. Do not mark a requirement PASS without running its verification. "
              "Create a checkpoint (`genius checkpoint`) before large edits."]
    text = redact("\n".join(lines) + "\n")
    if info.python:
        import shlex
        text = text.replace(shlex.quote(info.python), "python").replace(info.python, "python")
    out = p.gdir / "handoffs" / f"handoff-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text(text)
    p.store.execute("INSERT INTO handoffs(ts,path) VALUES(?,?)", (time.time(), str(out)))
    return text, out
