"""Project: ties together config, state DB, memory files, index, requirements, tasks, checkpoints, processes."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import Config, global_dir
from .db import Store
from .detect import ProjectInfo, detect_project
from .gitops import Checkpoints, Git
from .indexer import RepoIndex, is_test_path
from .procs import ProcessManager
from .requirements import Requirements, Tasks

MEMORY_FILES = {
    "architecture.md": "# Architecture\n\n_Not yet documented. Genius Dev fills this in after inspecting the project._\n",
    "decisions.md": "# Decisions\n\n",
    "progress.md": "# Progress\n\n",
    "bugs.md": "# Known bugs\n\n_None recorded._\n",
    "current_task.md": "# Current task\n\n_Idle._\n",
}


class UnsafeRoot(ValueError):
    pass


def _forbidden_root(p: Path) -> bool:
    return p == Path.home().resolve() or p == Path(p.anchor)


def find_root(start: Path) -> Path:
    """Nearest ancestor holding *Genius Dev* project memory (.genius/project.json or genius.db) or a .git; otherwise the start directory.
    A bare `.genius` directory (e.g. another tool's) is not a marker, and the home directory / filesystem root never is."""
    start = start.resolve()
    for p in [start, *start.parents]:
        if _forbidden_root(p):
            continue
        g = p / ".genius"
        if g.is_dir() and ((g / "project.json").exists() or (g / "genius.db").exists()):
            return p
    for p in [start, *start.parents]:
        if not _forbidden_root(p) and (p / ".git").exists():
            return p
    return start


def global_store() -> Store:
    return Store(global_dir() / "global.db")


class Project:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.gdir = self.root / ".genius"
        self.name = self.root.name
        self._info: ProjectInfo | None = None
        self.store: Store
        self.cfg = Config(self.root)
        self._index: RepoIndex | None = None

    @classmethod
    def open(cls, path: str | Path = ".", create: bool = True) -> "Project":
        p = cls(find_root(Path(path)))
        if create:
            p.init()
        return p

    @property
    def initialized(self) -> bool:
        return (self.gdir / "project.json").exists()

    # -- lifecycle -------------------------------------------------------------------
    def init(self) -> "Project":
        if _forbidden_root(self.root):
            raise UnsafeRoot(f"{self.root} is your home directory or the filesystem root — refusing to treat it as a project. cd into a project folder first.")
        self.gdir.mkdir(exist_ok=True)
        for d in ("checkpoints", "handoffs", "index", "logs", "screenshots"):
            (self.gdir / d).mkdir(exist_ok=True)
        self.store = Store(self.gdir / "genius.db")
        self.git = Git(self.root)
        if self.git.is_repo:
            self.git.exclude(".genius/")
        for name, body in MEMORY_FILES.items():
            f = self.gdir / name
            if not f.exists():
                f.write_text(body)
        if not (self.gdir / "requirements.md").exists():
            (self.gdir / "requirements.md").write_text("# Requirements\n\n_None yet._\n")
        if not (self.gdir / "project.json").exists():
            meta = {"name": self.name, "root": str(self.root), "created": time.time(), "info": self.info.to_dict()}
            (self.gdir / "project.json").write_text(json.dumps(meta, indent=2))
            self.store.execute("INSERT OR IGNORE INTO projects(name,root,created,info) VALUES(?,?,?,?)",
                               (self.name, str(self.root), time.time(), json.dumps(self.info.to_dict())))
        self._wire()
        return self

    def _wire(self) -> None:
        self.reqs = Requirements(self.store)
        self.tasks = Tasks(self.store)
        self.checkpoints = Checkpoints(self.store, Git(self.root), self.reqs)
        self.git = Git(self.root)
        self.procs = ProcessManager(self.store, self.root, self.gdir / "logs")

    @property
    def info(self) -> ProjectInfo:
        if self._info is None:
            self._info = detect_project(self.root)
        return self._info

    def pretty(self, cmd: str) -> str:
        """Display form of a command: the interpreter path (often long, with spaces) collapses to `python`."""
        import shlex
        py = self.info.python
        return cmd.replace(shlex.quote(py), "python").replace(py, "python") if py and cmd else cmd

    def redetect(self) -> ProjectInfo:
        self._info = None
        info = self.info
        if self.initialized:
            meta = json.loads((self.gdir / "project.json").read_text())
            meta["info"] = info.to_dict()
            (self.gdir / "project.json").write_text(json.dumps(meta, indent=2))
        return info

    @property
    def index(self) -> RepoIndex:
        if self._index is None:
            self._index = RepoIndex(self.root, self.gdir / "index", self.cfg.get("privacy.excluded"))
        return self._index

    # -- memory ----------------------------------------------------------------------
    def read_mem(self, name: str) -> str:
        try:
            return (self.gdir / name).read_text()
        except FileNotFoundError:
            return ""

    def append_mem(self, name: str, text: str) -> None:
        with open(self.gdir / name, "a") as f:
            f.write(text.rstrip() + "\n")

    def log_decision(self, title: str, detail: str = "", source: str = "agent") -> None:
        self.store.execute("INSERT INTO decisions(ts,title,detail,source) VALUES(?,?,?,?)", (time.time(), title, detail, source))
        self.append_mem("decisions.md", f"- **{time.strftime('%Y-%m-%d %H:%M')}** — {title}" + (f": {detail}" if detail else ""))

    def add_bug(self, title: str, detail: str = "") -> None:
        if self.store.one("SELECT 1 FROM bugs WHERE title=? AND status='open'", (title,)):
            return
        self.store.execute("INSERT INTO bugs(ts,title,detail) VALUES(?,?,?)", (time.time(), title, detail[:2000]))
        self.sync_memory()

    def resolve_bugs(self, like: str = "") -> None:
        self.store.execute("UPDATE bugs SET status='fixed' WHERE status='open' AND title LIKE ?", (f"%{like}%",))
        self.sync_memory()

    def remember(self, text: str) -> None:
        """Project-specific user preference; included in every context pack."""
        f = self.gdir / "preferences.md"
        if not f.exists():
            f.write_text("# Preferences\n\n")
        self.append_mem("preferences.md", f"- {text.strip()}")

    def preferences(self) -> list[str]:
        return [l[2:].strip() for l in self.read_mem("preferences.md").splitlines() if l.startswith("- ")]

    def write_architecture(self) -> None:
        """Regenerate the auto section of architecture.md (stack, structure, routes, tables, key modules); text below the marker is yours."""
        marker = "<!-- genius:notes -->"
        old = self.read_mem("architecture.md")
        notes = old.split(marker, 1)[1] if marker in old else ""
        info, ix = self.info, self.index
        lines = ["# Architecture", "", "_Auto-generated by Genius Dev from the repository index. Add your own notes below the marker._", "",
                 f"**Kind:** {info.kind} · **Languages:** {', '.join(info.languages) or '—'} · **Frameworks:** {', '.join(info.frameworks) or '—'}",
                 f"**Databases:** {', '.join(info.databases) or '—'} · **Services:** {', '.join(info.services) or '—'}",
                 f"**Test:** `{self.pretty(info.test_cmd) or '—'}` · **Build:** `{self.pretty(info.build_cmd) or '—'}` · **Dev:** `{self.pretty(info.dev_cmd) or '—'}`", ""]
        st = ix.stats()
        lines += ["## Index", f"{st['files']} files · {st['symbols']} symbols · {st['routes']} routes · {st['tables']} tables · {st['tests']} test files", ""]
        if any(info.structure.values()):
            lines += ["## Structure"] + [f"- {k}: {', '.join(v)}" for k, v in info.structure.items() if v] + [""]
        routes = ix.routes()
        if routes:
            lines += ["## API routes"] + [f"- `{r['method']} {r['route']}` — {r['path']}:{r['line']}" for r in routes[:40]] + [""]
        tables = ix.tables()
        if tables:
            lines += ["## Database tables / models"] + [f"- `{t['name']}` — {t['path']}:{t['line']}" for t in tables[:40]] + [""]
        big = sorted((f for f in ix.files() if not f["is_test"] and f["lang"] not in ("md", "json", "toml", "yaml", "yml", "lock")), key=lambda f: -f["lines"])[:10]
        if big:
            lines += ["## Largest source files"] + [f"- `{f['path']}` ({f['lines']} lines)" for f in big] + [""]
        by_file: dict[str, list[str]] = {}
        for sy in ix.symbols():
            if sy["kind"] in ("function", "class", "component", "type") and not sy["name"].startswith("_") and not is_test_path(sy["path"]):
                by_file.setdefault(sy["path"], []).append(sy["name"])
        if by_file:
            lines += ["## Key symbols"] + [f"- `{f}`: {', '.join(names[:8])}" for f, names in list(by_file.items())[:14]] + [""]
        (self.gdir / "architecture.md").write_text("\n".join(lines) + "\n" + marker + (notes if notes else "\n"))

    def set_current_task(self, text: str) -> None:
        (self.gdir / "current_task.md").write_text(f"# Current task\n\n{text}\n")

    def record_tests(self, kind: str, command: str, s, duration: float) -> None:
        self.store.execute("INSERT INTO test_runs(ts,kind,command,passed,failed,total,ok,duration,summary) VALUES(?,?,?,?,?,?,?,?,?)",
                           (time.time(), kind, command, s.passed, s.failed, s.total, int(s.ok), duration, "; ".join(s.failures[:5])))
        self.write_test_status()

    def last_run(self, kind: str) -> dict[str, Any] | None:
        r = self.store.one("SELECT * FROM test_runs WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,))
        return dict(r) if r else None

    def write_test_status(self) -> None:
        out = {k: self.last_run(k) for k in ("tests", "build", "lint", "typecheck", "browser")}
        (self.gdir / "test_status.json").write_text(json.dumps(out, indent=2, default=str))

    def sync_memory(self) -> None:
        """Regenerate derived markdown from the database so files never drift."""
        (self.gdir / "requirements.md").write_text(self.reqs.render_md())
        bugs = self.store.query("SELECT * FROM bugs WHERE status='open' ORDER BY id")
        (self.gdir / "bugs.md").write_text("# Known bugs\n\n" + ("\n".join(f"- {b['title']}" + (f"\n  {b['detail'][:300]}" if b['detail'] else "") for b in bugs) or "_None recorded._") + "\n")
        provs = {n: {k: v for k, v in c.to_dict().items() if k != "api_key_ref"} for n, c in self.cfg.providers().items()}
        (self.gdir / "providers.json").write_text(json.dumps({"mode": self.cfg.mode, "providers": provs,
                                                             "fallback": self.cfg.get("fallback.chain")}, indent=2))

    # -- sessions --------------------------------------------------------------------
    def start_session(self, goal: str) -> int:
        return self.store.execute("INSERT INTO sessions(started,goal,status) VALUES(?,?,'running')", (time.time(), goal)).lastrowid or 0

    def end_session(self, sid: int, status: str, summary: str = "") -> None:
        self.store.execute("UPDATE sessions SET ended=?, status=?, summary=? WHERE id=?", (time.time(), status, summary, sid))

    def last_session(self) -> dict[str, Any] | None:
        r = self.store.one("SELECT * FROM sessions ORDER BY id DESC LIMIT 1")
        return dict(r) if r else None

    def progress_pct(self) -> float:
        sm = self.reqs.summary()
        if sm.total:
            return sm.resolved_pct
        return self.tasks.progress()
