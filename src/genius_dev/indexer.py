"""Incremental repository index: files, symbols, routes, tables, imports, dependencies. Python uses the AST,
other languages use conservative regexes. Search is fuzzy/token based (no embeddings)."""
from __future__ import annotations

import ast
import hashlib
import os
import re
import sqlite3
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .secrets import DEFAULT_EXCLUDED_DIRS, is_secret_file

SRC_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".vue", ".svelte", ".go", ".rs", ".rb", ".php", ".java", ".kt", ".swift",
           ".dart", ".sql", ".html", ".css", ".scss", ".md", ".json", ".toml", ".yaml", ".yml", ".sh", ".c", ".h", ".cpp"}
MAX_FILE = 400_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, size INTEGER, lang TEXT, lines INTEGER, is_test INTEGER, sha TEXT);
CREATE TABLE IF NOT EXISTS symbols(id INTEGER PRIMARY KEY, path TEXT, name TEXT, kind TEXT, line INTEGER, sig TEXT);
CREATE TABLE IF NOT EXISTS routes(id INTEGER PRIMARY KEY, path TEXT, method TEXT, route TEXT, line INTEGER);
CREATE TABLE IF NOT EXISTS tables_(id INTEGER PRIMARY KEY, path TEXT, name TEXT, line INTEGER);
CREATE TABLE IF NOT EXISTS imports(path TEXT, module TEXT);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, path TEXT, kind TEXT, title TEXT, line INTEGER, text TEXT);
CREATE INDEX IF NOT EXISTS i_chunk ON chunks(path);
CREATE INDEX IF NOT EXISTS i_sym ON symbols(name); CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, path TEXT, kind TEXT, title TEXT, line INTEGER, text TEXT);
CREATE INDEX IF NOT EXISTS i_chunk ON chunks(path);
CREATE INDEX IF NOT EXISTS i_sympath ON symbols(path);
CREATE INDEX IF NOT EXISTS i_imp ON imports(path);
"""

_JS_SYM = [
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(", re.M), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.M), "class"),
    (re.compile(r"^\s*(?:export\s+)?const\s+([A-Z][\w$]*)\s*[:=]\s*(?:\([^)]*\)|[A-Za-z_$][\w$]*)?\s*(?:=>|\()", re.M), "component"),
    (re.compile(r"^\s*(?:export\s+)?const\s+([a-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*(?::\s*[^=]+)?=>", re.M), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.M), "type"),
]
_GENERIC_SYM = {
    ".go": [(re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)", re.M), "function"), (re.compile(r"^type\s+(\w+)\s+(?:struct|interface)", re.M), "type")],
    ".rs": [(re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", re.M), "function"), (re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)", re.M), "type")],
    ".rb": [(re.compile(r"^\s*def\s+(?:self\.)?(\w+[?!]?)", re.M), "function"), (re.compile(r"^\s*(?:class|module)\s+(\w+)", re.M), "class")],
    ".php": [(re.compile(r"^\s*(?:public |private |protected |static )*function\s+(\w+)", re.M), "function"), (re.compile(r"^\s*class\s+(\w+)", re.M), "class")],
    ".java": [(re.compile(r"^\s*(?:public |private |protected |static |final )*class\s+(\w+)", re.M), "class")],
    ".kt": [(re.compile(r"^\s*(?:private |internal |public )?(?:suspend )?fun\s+(?:\w+\.)?(\w+)", re.M), "function"), (re.compile(r"^\s*(?:data |sealed )?class\s+(\w+)", re.M), "class")],
    ".swift": [(re.compile(r"^\s*(?:public |private |internal |static |override )*func\s+(\w+)", re.M), "function"), (re.compile(r"^\s*(?:final )?(?:class|struct|enum|protocol)\s+(\w+)", re.M), "class")],
    ".dart": [(re.compile(r"^\s*class\s+(\w+)", re.M), "class")],
}
_ROUTES = [
    re.compile(r"""@(?:\w+)\.(get|post|put|delete|patch)\(\s*['"]([^'"]+)"""),               # FastAPI / Flask 2
    re.compile(r"""@\w+\.route\(\s*['"]([^'"]+)['"](?:.*methods=\[['"](\w+))?"""),            # Flask
    re.compile(r"""\b(?:app|router|api)\.(get|post|put|delete|patch)\(\s*['"`]([^'"`]+)"""),    # Express
    re.compile(r"""\bpath\(\s*['"]([^'"]*)['"]"""),                                            # Django
]
_SQL_TABLE = re.compile(r"(?i)create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"\[]?(\w+)")
_PY_TABLE = re.compile(r"__tablename__\s*=\s*['\"](\w+)['\"]")
_PRISMA_MODEL = re.compile(r"^model\s+(\w+)\s*\{", re.M)


def lang_of(path: str) -> str:
    return Path(path).suffix.lstrip(".").lower() or "text"


def is_test_path(rel: str) -> bool:
    n = rel.lower()
    return bool(re.search(r"(^|/)(tests?|__tests__|spec|e2e)/|(^|/)test_[^/]*$|[._-](test|spec)\.[a-z]+$|_test\.(go|py)$", n))


@dataclass
class Hit:
    path: str
    line: int = 0
    text: str = ""
    kind: str = "file"
    score: float = 0.0


class RepoIndex:
    def __init__(self, root: Path, index_dir: Path, excluded: Iterable[str] = DEFAULT_EXCLUDED_DIRS):
        self.root = root
        self.excluded = set(excluded)
        index_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(index_dir / "index.db"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # -- walking -------------------------------------------------------------------
    def walk(self) -> Iterable[Path]:
        for dp, dns, fns in os.walk(self.root):
            dns[:] = [d for d in dns if d not in self.excluded and not d.endswith(".egg-info")]
            for fn in fns:
                p = Path(dp) / fn
                if p.suffix.lower() in SRC_EXT and not is_secret_file(fn):
                    yield p

    # -- build ---------------------------------------------------------------------
    def refresh(self, force: bool = False) -> dict[str, int]:
        """Incremental: only re-parse files whose mtime/size changed. Returns counts."""
        known = {r["path"]: (r["mtime"], r["size"]) for r in self.db.execute("SELECT path,mtime,size FROM files")}
        seen: set[str] = set()
        changed = 0
        for p in self.walk():
            rel = str(p.relative_to(self.root))
            seen.add(rel)
            try:
                st = p.stat()
            except OSError:
                continue
            if not force and known.get(rel) == (st.st_mtime, st.st_size):
                continue
            if st.st_size > MAX_FILE:
                continue
            try:
                self._index_file(rel, p, st)
            except Exception:  # noqa: BLE001 — one unparseable file must never take the whole index (and the agent) down
                self._drop(rel)
                self.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?)", (rel, st.st_mtime, st.st_size, lang_of(rel), 0, int(is_test_path(rel)), ""))
            changed += 1
        removed = set(known) - seen
        for rel in removed:
            self._drop(rel)
        self.db.commit()
        return {"changed": changed, "removed": len(removed), "files": len(seen)}

    def _drop(self, rel: str) -> None:
        for t in ("files", "symbols", "routes", "tables_", "imports", "chunks"):
            self.db.execute(f"DELETE FROM {t} WHERE path=?", (rel,))

    def _index_file(self, rel: str, p: Path, st: os.stat_result) -> None:
        self._drop(rel)
        try:
            text = p.read_text(errors="replace")
        except OSError:
            return
        lang = lang_of(rel)
        self.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?)",
                        (rel, st.st_mtime, st.st_size, lang, text.count("\n") + 1, int(is_test_path(rel)),
                         hashlib.sha1(text.encode(errors="replace")).hexdigest()))
        syms: list[tuple[str, str, int, str]] = []
        imports: list[str] = []
        if lang == "py":
            syms, imports = self._py(text)
        elif lang in ("js", "jsx", "ts", "tsx", "mjs", "vue", "svelte"):
            for rx, kind in _JS_SYM:
                for m in rx.finditer(text):
                    syms.append((m.group(1), kind, text.count("\n", 0, m.start()) + 1, ""))
            imports = re.findall(r"""(?:import\s+[^'"]*?from\s+|import\s+|require\()\s*['"]([^'"]+)['"]""", text)
        elif "." + lang in _GENERIC_SYM:
            for rx, kind in _GENERIC_SYM["." + lang]:
                for m in rx.finditer(text):
                    syms.append((m.group(1), kind, text.count("\n", 0, m.start()) + 1, ""))
        for name, kind, sline, sig in syms:
            self.db.execute("INSERT INTO symbols(path,name,kind,line,sig) VALUES(?,?,?,?,?)", (rel, name, kind, sline, sig))
        self._chunks(rel, lang, text, syms)
        for mod in sorted(set(imports)):
            self.db.execute("INSERT INTO imports VALUES(?,?)", (rel, mod))
        if lang in ("py", "js", "jsx", "ts", "tsx", "mjs", "rb", "php", "go", "java", "kt"):
            for i, text_line in enumerate(text.splitlines(), 1):
                for rx in _ROUTES:
                    hit = False
                    for rm in rx.finditer(text_line):
                        groups = [x for x in rm.groups() if x is not None]
                        if rx is _ROUTES[3]:                                    # Django path('', view) is the site root
                            method, route = "ANY", (groups[0] or "/") if groups else "/"
                        else:
                            g = [x for x in groups if x]
                            if not g:
                                continue
                            method, route = (g[0].upper(), g[1]) if len(g) == 2 and g[0].lower() in ("get", "post", "put", "delete", "patch") else ("ANY", g[0])
                        self.db.execute("INSERT INTO routes(path,method,route,line) VALUES(?,?,?,?)", (rel, method, route, i))
                        hit = True
                    if hit:
                        break
        if lang in ("sql", "py", "prisma"):
            for rx in (_SQL_TABLE, _PY_TABLE):
                for m in rx.finditer(text):
                    self.db.execute("INSERT INTO tables_(path,name,line) VALUES(?,?,?)", (rel, m.group(1), text.count("\n", 0, m.start()) + 1))
        if rel.endswith(".prisma"):
            for m in _PRISMA_MODEL.finditer(text):
                self.db.execute("INSERT INTO tables_(path,name,line) VALUES(?,?,?)", (rel, m.group(1), text.count("\n", 0, m.start()) + 1))

    def _chunks(self, rel: str, lang: str, text: str, syms: list[tuple[str, str, int, str]]) -> None:
        """Retrieval units for semantic search: one chunk per symbol body, per markdown section, plus a file-head chunk."""
        lines = text.splitlines()
        kind_of = "test" if is_test_path(rel) else "doc" if lang == "md" else "code"
        add = lambda title, line, body, kind=kind_of: self.db.execute("INSERT INTO chunks(path,kind,title,line,text) VALUES(?,?,?,?,?)", (rel, kind, title, line, body[:1800]))
        if lang == "md":
            if not lines:
                return
            starts = [i for i, l in enumerate(lines) if l.startswith("#")] or [0]
            for a, b in zip(starts, starts[1:] + [len(lines)]):
                add(lines[a].lstrip("# ").strip() or rel, a + 1, "\n".join(lines[a:b]))
            return
        add(rel, 1, "\n".join(lines[:40]), "file")
        ordered = sorted(syms, key=lambda x: x[2])
        for i, (name, skind, line, sig) in enumerate(ordered):
            end = ordered[i + 1][2] - 1 if i + 1 < len(ordered) else len(lines)
            add(f"{skind} {name}", line, "\n".join(lines[line - 1: min(end, line + 60)]), "component" if skind == "component" else kind_of)

    @staticmethod
    def _py(text: str) -> tuple[list[tuple[str, str, int, str]], list[str]]:
        syms: list[tuple[str, str, int, str]] = []
        imports: list[str] = []
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")               # indexed files may contain invalid escapes etc.; that is their problem, not noise for us
                tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return syms, imports
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = ", ".join(a.arg for a in node.args.args)
                syms.append((node.name, "function", node.lineno, f"({args})"))
            elif isinstance(node, ast.ClassDef):
                syms.append((node.name, "class", node.lineno, ""))
            elif isinstance(node, ast.Import):
                imports += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        return syms, imports

    # -- queries -------------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        q = lambda t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return {"files": q("files"), "symbols": q("symbols"), "routes": q("routes"), "tables": q("tables_"),
                "tests": self.db.execute("SELECT COUNT(*) FROM files WHERE is_test=1").fetchone()[0]}

    def files(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM files ORDER BY path")]

    def symbols(self, path: str | None = None) -> list[dict[str, Any]]:
        if path:
            return [dict(r) for r in self.db.execute("SELECT * FROM symbols WHERE path=? ORDER BY line", (path,))]
        return [dict(r) for r in self.db.execute("SELECT * FROM symbols ORDER BY path, line")]

    def routes(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM routes ORDER BY route")]

    def tables(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM tables_ ORDER BY name")]

    def dependencies_of(self, path: str) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT module FROM imports WHERE path=?", (path,))]

    def related_tests(self, path: str) -> list[str]:
        stem = Path(path).stem.lower()
        return [r["path"] for r in self.db.execute("SELECT path FROM files WHERE is_test=1") if stem in r["path"].lower()]

    # -- fuzzy search --------------------------------------------------------------
    @staticmethod
    def fuzzy(query: str, target: str) -> float:
        q, t = query.lower(), target.lower()
        if not q:
            return 0.0
        if q == t:
            return 100.0
        if q in t:
            return 60.0 + 30.0 * len(q) / len(t)
        i = score = 0
        last = -2
        for ch in q:
            j = t.find(ch, i)
            if j < 0:
                return 0.0
            score += 3 if j == last + 1 else 1
            last, i = j, j + 1
        return score * 40.0 / (3 * len(q))

    def search(self, query: str, limit: int = 30, kinds: tuple[str, ...] = ("file", "symbol", "route", "table")) -> list[Hit]:
        hits: list[Hit] = []
        if "file" in kinds:
            for r in self.db.execute("SELECT path FROM files"):
                s = self.fuzzy(query, r["path"])
                if s > 0:
                    hits.append(Hit(r["path"], 0, r["path"], "file", s))
        if "symbol" in kinds:
            for r in self.db.execute("SELECT path,name,kind,line FROM symbols"):
                s = self.fuzzy(query, r["name"])
                if s > 0:
                    hits.append(Hit(r["path"], r["line"], f"{r['kind']} {r['name']}", "symbol", s + 5))
        if "route" in kinds:
            for r in self.db.execute("SELECT path,method,route,line FROM routes"):
                s = self.fuzzy(query, r["route"])
                if s > 0:
                    hits.append(Hit(r["path"], r["line"], f"{r['method']} {r['route']}", "route", s))
        if "table" in kinds:
            for r in self.db.execute("SELECT path,name,line FROM tables_"):
                s = self.fuzzy(query, r["name"])
                if s > 0:
                    hits.append(Hit(r["path"], r["line"], f"table {r['name']}", "table", s))
        hits.sort(key=lambda h: -h.score)
        return hits[:limit]

    def grep(self, pattern: str, limit: int = 50, glob: str | None = None, regex: bool = False) -> list[Hit]:
        rx = re.compile(pattern if regex else re.escape(pattern), re.I)
        out: list[Hit] = []
        for r in self.db.execute("SELECT path FROM files"):
            if glob and not Path(r["path"]).match(glob):
                continue
            try:
                for i, line in enumerate((self.root / r["path"]).read_text(errors="replace").splitlines(), 1):
                    if rx.search(line):
                        out.append(Hit(r["path"], i, line.strip()[:200], "text"))
                        if len(out) >= limit:
                            return out
            except OSError:
                continue
        return out

    def relevant_files(self, text: str, limit: int = 8) -> list[tuple[str, float]]:
        """Rank files for a natural-language task by token overlap with paths, symbols and content head."""
        allt = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower()))
        toks = {t for t in allt if t not in _STOP} or allt          # never let stop-words erase the whole query ("fix add")
        if not toks:
            return []
        scores: dict[str, float] = {}
        for r in self.db.execute("SELECT path FROM files"):
            pl = r["path"].lower()
            s = sum(3.0 for t in toks if t in pl)
            if s:
                scores[r["path"]] = s
        for r in self.db.execute("SELECT path,name FROM symbols"):
            nl = r["name"].lower()
            s = sum(2.0 for t in toks if t in nl or nl in toks)
            if s:
                scores[r["path"]] = scores.get(r["path"], 0) + s
        try:                                                    # blend in local semantic (BM25 + concept expansion) ranking
            from .semantic import semantic_files
            for path, sc in semantic_files(self, text, limit * 2):
                scores[path] = scores.get(path, 0) + sc
        except Exception:  # noqa: BLE001
            pass
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        return ranked


_STOP = set("the and for with that this from into make sure fix add build create when then have has not are was can all any get run use "
            "should would please need want app page code file works work working".split())
