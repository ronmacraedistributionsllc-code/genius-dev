"""Git integration and non-intrusive checkpoints (private refs; never touches the user's branch, index or history)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .db import Store
from .secrets import is_secret_file

ID_ENV = {"GIT_AUTHOR_NAME": "Genius Dev", "GIT_AUTHOR_EMAIL": "genius@localhost",
          "GIT_COMMITTER_NAME": "Genius Dev", "GIT_COMMITTER_EMAIL": "genius@localhost"}


JUNK_DIRS = ["__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".venv", "venv", ".genius", ".next", ".gradle", "Pods"]


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, root: Path):
        self.root = root

    def run(self, *args: str, env: dict[str, str] | None = None, check: bool = True, input: bytes | None = None) -> str:
        r = subprocess.run(["git", *args], cwd=self.root, capture_output=True, input=input,
                           env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", **(env or {})})
        if check and r.returncode != 0:
            raise GitError(r.stderr.decode(errors="replace").strip() or f"git {' '.join(args)} failed")
        return r.stdout.decode(errors="replace")

    def ok(self, *args: str) -> bool:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True).returncode == 0

    @property
    def is_repo(self) -> bool:
        return self.ok("rev-parse", "--git-dir")

    def init(self) -> None:
        self.run("init", "-q")
        self.exclude(".genius/")

    def exclude(self, pattern: str) -> None:
        gd = Path(self.run("rev-parse", "--git-dir").strip())
        gd = gd if gd.is_absolute() else self.root / gd
        f = gd / "info" / "exclude"
        f.parent.mkdir(exist_ok=True)
        cur = f.read_text() if f.exists() else ""
        if pattern not in cur.splitlines():
            f.write_text(cur + ("" if cur.endswith("\n") or not cur else "\n") + pattern + "\n")

    def empty_tree(self) -> str:
        return self.run("hash-object", "-t", "tree", "/dev/null").strip()

    # -- read ---------------------------------------------------------------------
    def branch(self) -> str:
        return self.run("symbolic-ref", "--short", "-q", "HEAD", check=False).strip() or "(detached)"

    def head(self) -> str:
        r = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=self.root, capture_output=True)
        return r.stdout.decode().strip() if r.returncode == 0 else ""

    def status(self) -> list[tuple[str, str]]:
        out = self.run("status", "--porcelain", "-uall", check=False)
        return [(l[:2].strip() or "?", l[3:]) for l in out.splitlines() if l]

    def diff(self, stat: bool = False, base: str | None = None) -> str:
        """Diff of working tree (incl. untracked) vs ``base`` (default HEAD)."""
        tree = self.snapshot_tree()
        base_tree = (base or (self.head() or self.empty_tree()))  # empty tree if no commits
        args = ["diff", "--no-color", "--no-ext-diff", base_tree, tree]
        if stat:
            args.insert(1, "--stat")
        return self.run(*args, check=False)

    def numstat(self, base: str | None = None) -> list[tuple[str, int, int]]:
        tree = self.snapshot_tree()
        base_tree = base or self.head() or self.empty_tree()
        rows = []
        for l in self.run("diff", "--numstat", "--no-renames", base_tree, tree, check=False).splitlines():
            a, d, p = l.split("\t", 2)
            rows.append((p, int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0))
        return rows

    def log(self, n: int = 10) -> list[str]:
        return self.run("log", f"-{n}", "--pretty=%h %s", check=False).splitlines()

    def branches(self) -> list[str]:
        return [b.strip("* ").strip() for b in self.run("branch", check=False).splitlines()]

    # -- snapshots ----------------------------------------------------------------
    def snapshot_tree(self) -> str:
        """Write a tree of the working directory (tracked + untracked, minus ignored and secret files)."""
        with tempfile.TemporaryDirectory() as td:
            env = {"GIT_INDEX_FILE": os.path.join(td, "idx")}
            excl = [f":(exclude,glob)**/{d}/**" for d in JUNK_DIRS] + [f":(exclude,glob){d}/**" for d in JUNK_DIRS]
            self.run("add", "-A", "--", ".", *excl, env=env, check=False)
            names = self.run("ls-files", "-z", env=env).split("\0")
            bad = [n for n in names if n and is_secret_file(n)]
            for n in bad:
                self.run("update-index", "--force-remove", "--", n, env=env, check=False)
            return self.run("write-tree", env=env).strip()

    def commit_snapshot(self, message: str, ref: str) -> tuple[str, str]:
        tree = self.snapshot_tree()
        args = ["commit-tree", tree, "-m", message]
        head = self.head()
        if head:
            args += ["-p", head]
        commit = self.run(*args, env=ID_ENV).strip()
        self.run("update-ref", ref, commit)
        return commit, tree

    def restore_tree(self, commit: str) -> dict[str, list[str]]:
        """Make the working tree match ``commit``. Only files that differ are touched; ignored & secret files are left alone."""
        current = self.snapshot_tree()
        changed = self.run("diff-tree", "-r", "-z", "--name-status", "--no-renames", current, commit).split("\0")
        restored, deleted = [], []
        pairs = [(changed[i], changed[i + 1]) for i in range(0, len(changed) - 1, 2) if changed[i]]
        with tempfile.TemporaryDirectory() as td:
            env = {"GIT_INDEX_FILE": os.path.join(td, "idx")}
            self.run("read-tree", commit, env=env)
            to_checkout = [p for s, p in pairs if s in ("A", "M", "T")]
            if to_checkout:
                self.run("checkout-index", "-f", "-z", "--stdin", env=env, input=("\0".join(to_checkout) + "\0").encode())
                restored = to_checkout
        for s, p in pairs:
            if s == "D":
                f = self.root / p
                if f.exists() and not is_secret_file(p):
                    f.unlink()
                    deleted.append(p)
                    d = f.parent
                    while d != self.root and d.exists() and not any(d.iterdir()):
                        d.rmdir()
                        d = d.parent
        return {"restored": restored, "deleted": deleted}


class Checkpoints:
    def __init__(self, store: Store, git: Git, reqs=None):
        self.s, self.git, self.reqs = store, git, reqs

    def _next_id(self) -> str:
        n = self.s.scalar("SELECT COUNT(*) FROM checkpoints") + 1
        return f"cp-{n:04d}"

    def create(self, task: str = "", kind: str = "auto", tests: dict | None = None) -> dict[str, Any]:
        if not self.git.is_repo:
            self.git.init()
        cid = self._next_id()
        commit, tree = self.git.commit_snapshot(f"genius checkpoint {cid}: {task}"[:200], f"refs/genius/cp/{cid}")
        changed = [p for p, _, _ in self.git.numstat()][:200]
        reqs = json.dumps([{"id": r["id"], "status": r["status"]} for r in self.reqs.all()]) if self.reqs else "[]"
        mig = sorted(str(p.relative_to(self.git.root)) for d in ("migrations", "alembic/versions", "prisma/migrations", "supabase/migrations", "db/migrate")
                     for p in (self.git.root / d).glob("*") if p.is_file() or p.is_dir())[:100]
        self.s.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?,?,?)",
                       (cid, time.time(), commit, task, reqs, json.dumps(tests or {}), json.dumps(changed), json.dumps(mig), kind))
        return self.get(cid)

    def get(self, cid: str) -> dict[str, Any]:
        r = self.s.one("SELECT * FROM checkpoints WHERE id=?", (cid,))
        if not r:
            raise KeyError(cid)
        d = dict(r)
        for k in ("requirements", "tests", "files_changed", "migrations"):
            d[k] = json.loads(d[k] or "null")
        return d

    def list(self) -> list[dict[str, Any]]:
        return [self.get(r["id"]) for r in self.s.query("SELECT id FROM checkpoints ORDER BY ts DESC")]

    def restore(self, cid: str) -> dict[str, Any]:
        cp = self.get(cid)
        safety = self.create(f"before restore of {cid}", kind="pre-restore")     # restore is itself undoable
        res = self.git.restore_tree(cp["commit_hash"])
        if self.reqs:
            for r in cp["requirements"] or []:
                self.reqs.s.execute("UPDATE requirements SET status=? WHERE id=?", (r["status"], r["id"]))
        return {**res, "checkpoint": cp["id"], "safety_checkpoint": safety["id"]}

    def undo(self) -> dict[str, Any]:
        """Return to the most recent checkpoint that differs from the current working tree."""
        current = self.git.snapshot_tree()
        for cp in self.list():
            if cp["kind"] == "pre-restore":
                continue
            tree = self.git.run("rev-parse", cp["commit_hash"] + "^{tree}").strip()
            if tree != current:
                return self.restore(cp["id"])
        raise GitError("nothing to undo — the working tree already matches the latest checkpoint")
