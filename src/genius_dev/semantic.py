"""Offline semantic project search — no embedding API, no network.

Method: BM25 over retrieval chunks (symbol bodies, markdown sections, file heads, plus requirements and tasks),
with identifier-aware tokenisation (camelCase / snake_case split, light stemming), a built-in concept thesaurus
(query "authentication" also finds login/session/token/password code), typo tolerance via vocabulary fuzzy-matching,
and title/path field boosting. This is lexical-semantic retrieval, not neural embeddings: it understands the
*vocabulary of software*, not arbitrary paraphrase. See ARCHITECTURE.md.
"""
from __future__ import annotations

import difflib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

CONCEPTS: dict[str, set[str]] = {
    "auth": {"auth", "authentication", "authenticate", "authorize", "authorization", "login", "logout", "signin", "signup", "register", "password", "passwd", "credential", "session", "token", "jwt", "oauth", "cookie", "permission", "role", "user", "account"},
    "password": {"password", "passwd", "reset", "forgot", "hash", "bcrypt", "argon", "salt", "credential", "recover"},
    "payment": {"payment", "pay", "charge", "billing", "invoice", "stripe", "checkout", "card", "subscription", "price", "refund", "order"},
    "database": {"database", "db", "sql", "query", "table", "model", "schema", "migration", "orm", "sqlite", "postgres", "mysql", "record", "row", "column", "index"},
    "api": {"api", "endpoint", "route", "router", "handler", "controller", "request", "response", "http", "rest", "url", "path", "view"},
    "test": {"test", "spec", "assert", "expect", "fixture", "mock", "coverage", "unittest", "pytest", "jest"},
    "error": {"error", "exception", "raise", "throw", "fail", "failure", "bug", "traceback", "catch", "handle", "retry"},
    "config": {"config", "configuration", "setting", "settings", "env", "environment", "option", "flag", "toml", "yaml", "json"},
    "ui": {"ui", "component", "page", "view", "screen", "render", "button", "form", "layout", "style", "css", "template", "widget"},
    "cache": {"cache", "memo", "memoize", "ttl", "redis", "store", "lru"},
    "file": {"file", "path", "read", "write", "upload", "download", "directory", "folder", "fs", "stream"},
    "log": {"log", "logger", "logging", "trace", "debug", "audit", "event", "metric"},
    "email": {"email", "mail", "smtp", "message", "notification", "notify", "send", "template"},
    "search": {"search", "find", "query", "filter", "index", "match", "fuzzy", "rank"},
    "merchant": {"merchant", "seller", "vendor", "shop", "store", "business", "tenant", "organization", "org"},
    "delete": {"delete", "remove", "destroy", "drop", "clean", "purge"},
    "create": {"create", "add", "new", "insert", "make", "build", "generate", "register"},
}
STOP = set("a an the and or of to in on for with from by at as is are was were be been it its this that these those where how what which who when why does do did "
           "handled handle handles handling work works working code function method class file implemented implement implementation used use using find show me get "
           "i we you they our my your there here can could should would please".split())
_SPLIT = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def stem(t: str) -> str:
    for suf in ("ization", "ation", "ing", "ies", "ed", "es", "er", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        parts = [w.lower() for w in _SPLIT.findall(word.replace("_", " ")) if len(w) > 1]
        if len(parts) > 1:
            out.append(word.lower().replace("_", ""))          # keep the whole identifier too
        out.extend(parts)
    return [stem(t) for t in out if t not in STOP and len(t) > 1]


_CONCEPT_STEMS = {k: {stem(x) for x in v} for k, v in CONCEPTS.items()}


def expand(tokens: list[str], vocab: set[str]) -> dict[str, float]:
    """Query token → weight. Own tokens weigh 1.0, concept siblings 0.35, close spelling matches 0.8."""
    w: dict[str, float] = {}
    for t in tokens:
        if t in vocab:
            w[t] = max(w.get(t, 0), 1.0)
        else:
            close = difflib.get_close_matches(t, vocab, n=1, cutoff=0.82)
            if close:
                w[close[0]] = max(w.get(close[0], 0), 0.8)
        for group in _CONCEPT_STEMS.values():
            if t in group:
                for sib in group:
                    if sib != t and sib in vocab:
                        w[sib] = max(w.get(sib, 0), 0.35)
    return w


@dataclass
class Chunk:
    path: str
    kind: str
    title: str
    line: int
    text: str


class Model:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.tf: list[Counter] = []
        self.len: list[int] = []
        df: Counter = Counter()
        for c in chunks:
            toks = tokenize(c.text) + tokenize(c.title) * 3 + tokenize(c.path) * 2          # field boost
            cnt = Counter(toks)
            self.tf.append(cnt)
            self.len.append(sum(cnt.values()) or 1)
            df.update(cnt.keys())
        self.df, self.n = df, max(1, len(chunks))
        self.avg = sum(self.len) / self.n if chunks else 1
        self.vocab = set(df)

    def search(self, query: str, k: int = 10, kinds: set[str] | None = None) -> list[tuple[float, Chunk]]:
        q = expand(tokenize(query), self.vocab)
        if not q:
            return []
        scored: list[tuple[float, Chunk]] = []
        for i, c in enumerate(self.chunks):
            if kinds and c.kind not in kinds:
                continue
            tf, dl, s = self.tf[i], self.len[i], 0.0
            for t, wt in q.items():
                f = tf.get(t, 0)
                if f:
                    idf = math.log(1 + (self.n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                    s += wt * idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * dl / self.avg))
            if s > 0:
                scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        return scored[:k]


_CACHE: dict[str, tuple[tuple, Model]] = {}


def _load(index) -> Model:
    stamp = tuple(index.db.execute("SELECT COUNT(*), COALESCE(MAX(mtime),0), COALESCE(SUM(size),0) FROM files").fetchone())
    key = str(index.root)
    hit = _CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    rows = index.db.execute("SELECT path,kind,title,line,text FROM chunks").fetchall()
    m = Model([Chunk(r["path"], r["kind"], r["title"], r["line"], r["text"]) for r in rows])
    _CACHE[key] = (stamp, m)
    return m


def semantic_search(index, query: str, k: int = 10, kinds: set[str] | None = None) -> list[dict[str, Any]]:
    out = []
    for score, c in _load(index).search(query, k, kinds):
        first = next((l.strip() for l in c.text.splitlines() if l.strip() and not l.strip().startswith(("#", "//", '"""'))), c.title)
        out.append({"score": round(score, 2), "path": c.path, "line": c.line, "kind": c.kind, "title": c.title, "snippet": first[:110]})
    return out


def semantic_files(index, query: str, k: int = 10) -> list[tuple[str, float]]:
    """File-level scores (best chunk per file, normalised to ~0–6) for blending into context selection."""
    best: dict[str, float] = {}
    hits = _load(index).search(query, k * 3)
    top = hits[0][0] if hits else 1
    for s, c in hits:
        best[c.path] = max(best.get(c.path, 0), 6 * s / top)
    return sorted(best.items(), key=lambda kv: -kv[1])[:k]


def semantic_extra(query: str, requirements: list[dict], tasks: list[dict], k: int = 5) -> list[dict[str, Any]]:
    """Requirements and tasks are searched semantically too (tiny, built per query)."""
    chunks = [Chunk("", "requirement", r["id"], 0, f"{r['description']} {r.get('verify', '')}") for r in requirements]
    chunks += [Chunk("", "task", t["title"], 0, t["title"]) for t in tasks]
    if not chunks:
        return []
    return [{"score": round(s, 2), "path": "", "line": 0, "kind": c.kind, "title": c.title, "snippet": c.text[:110]} for s, c in Model(chunks).search(query, k)]
