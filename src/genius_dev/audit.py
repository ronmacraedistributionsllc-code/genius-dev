"""genius finish / security / review — deep completion audit and static security & code checks."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import REVIEW_SYSTEM, extract_json
from .project import Project
from .events import EventBus
from .runner import truncate
from .secrets import find_secrets, is_secret_file
from .tools import ToolBox

SEV = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Finding:
    kind: str
    severity: str        # high | medium | low
    message: str
    path: str = ""
    line: int = 0
    fixable: bool = True

    def text(self) -> str:
        loc = f"{self.path}:{self.line}  " if self.path else ""
        return f"[{self.severity}] {self.kind}: {loc}{self.message}"


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)
    gates: dict[str, str] = field(default_factory=dict)
    requirements: str = ""
    ui: str = ""
    passed: bool = False
    iterations: int = 0
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "gates": self.gates, "requirements": self.requirements, "iterations": self.iterations,
                "blockers": self.blockers, "ui": self.ui, "findings": [f.__dict__ for f in self.findings]}

    def text(self) -> str:
        lines = ["FINISH AUDIT — " + ("PASS" if self.passed else "NOT DONE"), "", "REQUIREMENTS  " + self.requirements]
        lines += [f"{k.upper():13} {v}" for k, v in self.gates.items()]
        if self.ui:
            lines += ["", self.ui]
        if self.findings:
            lines += ["", f"FINDINGS ({len(self.findings)})"] + [f"  {f.text()}" for f in sorted(self.findings, key=lambda f: SEV[f.severity])[:40]]
        if self.blockers:
            lines += ["", "BLOCKERS"] + [f"  · {b}" for b in self.blockers]
        return "\n".join(lines)


_MARKERS = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")  # genius:ignore
_PLACEHOLDER = re.compile(r"(?i)lorem ipsum|placeholder data|dummy data|your[_ -]?api[_ -]?key[_ -]?here|changeme|example\.com|foo@bar")  # genius:ignore
_MOCKISH = re.compile(r"(?i)\b(mock_?data|fake_?data|mockApi|MOCK_)\w*")  # genius:ignore
_EMPTY_HANDLER = re.compile(r"""on[Cc]lick=\{\s*\(\s*\)\s*=>\s*\{\s*\}\s*\}|onclick=["']\s*["']|href=["'](?:#|javascript:void\(0\))?["']""")
_NOT_IMPL = re.compile(r"raise NotImplementedError|throw new Error\(['\"]not implemented|unimplemented!\(|todo!\(")  # genius:ignore
_CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".html", ".go", ".rs", ".rb", ".php", ".java", ".kt", ".swift", ".dart"}

_SECURITY_RULES = [
    (re.compile(r"\beval\s*\("), "medium", "use of eval()"),
    (re.compile(r"\bexec\s*\("), "medium", "use of exec()"),
    (re.compile(r"shell\s*=\s*True"), "medium", "subprocess with shell=True"),
    (re.compile(r"pickle\.loads?\("), "medium", "unpickling untrusted data can execute code"),
    (re.compile(r"verify\s*=\s*False"), "high", "TLS verification disabled"),
    (re.compile(r"(?i)\.innerHTML\s*="), "medium", "innerHTML assignment (XSS risk)"),
    (re.compile(r"dangerouslySetInnerHTML"), "medium", "dangerouslySetInnerHTML (XSS risk)"),
    (re.compile(r"""(?i)(execute|query)\(\s*f["'].*\b(select|insert|update|delete)\b.*\{"""), "high", "SQL built with an f-string (injection risk)"),
    (re.compile(r"""(?i)["'](select|insert|update|delete)\b[^"']*["']\s*\+\s*\w+"""), "high", "SQL built by string concatenation"),
    (re.compile(r"(?i)DEBUG\s*=\s*True"), "low", "DEBUG enabled"),
    (re.compile(r"""(?i)Access-Control-Allow-Origin["']?\s*[:,]\s*["']\*"""), "medium", "wildcard CORS"),
    (re.compile(r"\b(md5|sha1)\s*\("), "low", "weak hash function (fine for checksums, not passwords)"),
]


def _scan_files(p: Project, tests: bool = False):
    for f in p.index.files():
        rel = f["path"]
        if Path(rel).suffix not in _CODE_EXT and not rel.endswith((".env.example", ".md")):
            continue
        if f["is_test"] and not tests:
            continue
        try:
            yield rel, (p.root / rel).read_text(errors="replace")
        except OSError:
            continue


def _pass_stubs(rel: str, text: str) -> list[Finding]:
    """Functions whose entire body is `pass` / `...` (except abstract methods, overloads and protocol members)."""
    import ast
    out: list[Finding] = []
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return out
    protocol_lines: set[int] = set()
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        bases = {getattr(b, "id", getattr(b, "attr", "")) for b in cls.bases}
        if bases & {"Protocol", "ABC", "Exception", "BaseException"} or any(getattr(getattr(k, "value", None), "id", "") == "ABCMeta" for k in cls.keywords):
            protocol_lines.update(range(cls.lineno, (cls.end_lineno or cls.lineno) + 1))
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        decos = {getattr(d, "id", getattr(d, "attr", "")) for d in fn.decorator_list}
        if decos & {"abstractmethod", "overload"} or fn.lineno in protocol_lines:
            continue
        def is_doc(b: ast.stmt) -> bool:
            return isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and isinstance(b.value.value, str)

        def is_empty(b: ast.stmt) -> bool:
            return isinstance(b, ast.Pass) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and b.value.value is Ellipsis)
        body = [b for b in fn.body if not is_doc(b)]
        if body and all(is_empty(b) for b in body):
            out.append(Finding("stub", "medium", f"function `{fn.name}` has an empty body (pass / ...)", rel, fn.lineno))
    return out


def static_findings(p: Project) -> list[Finding]:
    out: list[Finding] = []
    for rel, text in _scan_files(p):
        if rel.endswith(".md"):
            continue
        if rel.endswith(".py"):
            out += _pass_stubs(rel, text)
        for i, line in enumerate(text.splitlines(), 1):
            if "genius:ignore" in line:                       # deliberate: e.g. the audit's own pattern definitions
                continue
            if _MARKERS.search(line):
                out.append(Finding("todo", "low", line.strip()[:100], rel, i))
            if _PLACEHOLDER.search(line):
                out.append(Finding("placeholder", "medium", line.strip()[:100], rel, i))
            if _MOCKISH.search(line):
                out.append(Finding("mock", "medium", "mock/fake data or API reference in production code: " + line.strip()[:80], rel, i))
            if _EMPTY_HANDLER.search(line):
                out.append(Finding("dead-ui", "medium", "empty handler or dead link: " + line.strip()[:80], rel, i))
            if _NOT_IMPL.search(line):
                out.append(Finding("unimplemented", "high", "unimplemented stub", rel, i))
    out += secret_findings(p)
    return out


def secret_findings(p: Project) -> list[Finding]:
    """Per-line scan. Obviously fabricated tokens (docs/tests: alphabet runs, 'secret', 'fake', …) and lines marked `genius:ignore` are skipped."""
    from .secrets import _PATTERNS, looks_fake
    out: list[Finding] = []
    for rel, text in _scan_files(p, tests=True):
        if not find_secrets(text):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if "genius:ignore" in line:
                continue
            for kind, rx in _PATTERNS:
                m = rx.search(line)
                if m and not looks_fake(m.group(m.lastindex or 0)):
                    if kind == "assignment" and rel.endswith((".example", ".md")):
                        continue
                    out.append(Finding("secret", "high", f"possible hardcoded credential ({kind})", rel, i))
                    break
    if p.git.is_repo:
        tracked = p.git.run("ls-files", check=False).splitlines()
        for t in tracked:
            if is_secret_file(t) and not t.endswith((".example", ".sample")):
                out.append(Finding("secret", "high", "secret file is tracked by git", t, 0, True))
    gi = p.root / ".gitignore"
    if (p.root / ".env").exists() and (not gi.exists() or ".env" not in gi.read_text()):
        out.append(Finding("secret", "high", ".env exists but is not in .gitignore", ".gitignore", 0))
    return out


def security_findings(p: Project) -> list[Finding]:
    out = secret_findings(p)
    for rel, text in _scan_files(p):
        for i, line in enumerate(text.splitlines(), 1):
            for rx, sev, msg in _SECURITY_RULES:
                if rx.search(line):
                    out.append(Finding("security", sev, msg, rel, i, False))
    return out


async def run_finish(p: Project, tools: ToolBox, router, bus: EventBus, agent=None, fix: bool = True, max_iters: int = 3) -> AuditReport:
    """Audit, repair via the agent, and repeat until PASS, a legitimate blocker, or no progress."""
    rep = AuditReport()
    prev: set[str] = set()
    for it in range(1, max_iters + 1):
        rep = await _audit_once(p, tools, router, bus, agent)
        rep.iterations = it
        sig = {f.text() for f in rep.findings} | {k for k, v in rep.gates.items() if v.startswith("FAIL")}
        bus.emit("AGENT", "AUDIT", f"pass {it}: {len(rep.findings)} findings, {'PASS' if rep.passed else 'not done'}")
        if rep.passed or not fix or agent is None:
            break
        if sig == prev:
            rep.blockers.append("no progress between audit passes — remaining items need a human decision")
            break
        prev = sig
        todo = [f.text() for f in rep.findings if f.fixable and f.severity != "low"][:15]
        todo += [f"{k} gate failing" for k, v in rep.gates.items() if v.startswith("FAIL")]
        if not todo:
            break
        await agent.run("Resolve these audit findings without breaking existing behaviour:\n" + "\n".join(todo))
    return rep


async def _audit_once(p: Project, tools: ToolBox, router, bus: EventBus, agent=None) -> AuditReport:
    await asyncio.to_thread(p.index.refresh)
    rep = AuditReport()
    gates_done: dict[str, Any] = {}
    if agent is not None and p.reqs.summary().total:
        agent.tools = tools
        await agent.verify_only()                          # statuses come from fresh evidence, never from what was stored earlier
        gates_done = dict(agent.last_gates)
    sm = p.reqs.summary()
    c = sm.counts
    rep.requirements = f"{c['PASS']} PASS · {c['FAIL']} FAIL · {c['BLOCKED']} BLOCKED · {c['WAIVED']} WAIVED · {c['NOT_STARTED'] + c['IN_PROGRESS']} open"
    if sm.total == 0:
        rep.findings.append(Finding("requirements", "medium", "no requirements recorded — completion cannot be verified", fixable=False))
    for r in p.reqs.all():
        if r["status"] in ("FAIL", "NOT_STARTED", "IN_PROGRESS"):
            rep.findings.append(Finding("requirement", "high", f"{r['id']} {r['status']}: {r['description'][:80]}"))
        if r["status"] == "BLOCKED":
            rep.blockers.append(f"{r['id']}: {r['result'] or r['description']}")
    info = p.info
    for kind, cmd in (("tests", info.test_cmd), ("build", info.build_cmd), ("lint", info.lint_cmd), ("typecheck", info.typecheck_cmd)):
        if not cmd:
            rep.gates[kind] = "n/a (none detected)"
            continue
        gate = gates_done.get(kind) or await tools.execute(kind, {})
        rep.gates[kind] = ("PASS " if gate.ok else "FAIL ") + gate.summary
        if not gate.ok:
            rep.findings.append(Finding(kind, "high", gate.summary + " — " + truncate(gate.output, 200).replace("\n", " ")))
    rep.findings += static_findings(p)
    rep.findings += project_checks(p)
    if info.is_web:
        from .visual import visual_qa
        vr = await visual_qa(p, tools, router, bus)
        rep.ui = vr.text()
        if vr.ran:
            rep.findings += [Finding("ui", "high", f, fixable=True) for f in vr.failures[:15]]
            rep.findings += [Finding("console", "high", c_, fixable=True) for c_ in vr.console_errors[:8]]
            rep.findings += [Finding("network", "medium", n) for n in vr.failed_requests[:8]]
            rep.findings += [Finding("ui", "low", w) for w in vr.warnings[:10]]
        else:
            rep.blockers.append(vr.text())
    blocking = [f for f in rep.findings if f.severity in ("high", "medium")]
    rep.passed = not blocking and all(not v.startswith("FAIL") for v in rep.gates.values()) and sm.complete
    return rep


def project_checks(p: Project) -> list[Finding]:
    """Documentation and packaging sanity: README present and mentions the CLI entry points; declared entry points import."""
    out: list[Finding] = []
    readme = next((p.root / n for n in ("README.md", "README.rst", "README.txt", "README") if (p.root / n).exists()), None)
    if readme is None:
        out.append(Finding("docs", "medium", "no README — nobody can tell how to run this project", fixable=True))
    elif len(readme.read_text(errors="replace").strip()) < 150:
        out.append(Finding("docs", "low", "README is nearly empty", readme.name, 0))
    pj = p.root / "pyproject.toml"
    if pj.exists():
        import tomllib
        try:
            data = tomllib.loads(pj.read_text())
        except tomllib.TOMLDecodeError as e:
            return out + [Finding("packaging", "high", f"pyproject.toml is invalid TOML: {e}", "pyproject.toml", 0, False)]
        proj = data.get("project", {})
        for key in ("name", "version"):
            if key not in proj and "dynamic" not in proj:
                out.append(Finding("packaging", "medium", f"pyproject.toml [project] has no {key}", "pyproject.toml", 0))
        for script, target in (proj.get("scripts") or {}).items():
            mod, _, attr = target.partition(":")
            import subprocess, shlex
            code = f"import importlib; m=importlib.import_module({mod!r}); getattr(m, {attr!r})" if attr else f"import importlib; importlib.import_module({mod!r})"
            r = subprocess.run(f"{shlex.quote(p.info.python or 'python3')} -c {shlex.quote(code)}", shell=True, cwd=p.root, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                out.append(Finding("packaging", "high", f"entry point `{script} = {target}` cannot be imported: {(r.stderr.strip().splitlines() or ['?'])[-1][:90]}", "pyproject.toml", 0, False))
            if readme is not None and script not in readme.read_text(errors="replace"):
                out.append(Finding("docs", "low", f"README never mentions the `{script}` command", readme.name, 0))
    return out


async def review_diff(p: Project, router) -> dict[str, Any]:
    diff = p.git.diff() if p.git.is_repo else ""
    if not diff.strip():
        return {"verdict": "pass", "findings": [], "summary": "No changes to review."}
    comp = await router.complete("reviewer", [{"role": "user", "content": truncate(diff, 16000, keep_tail=False)}], REVIEW_SYSTEM, json_mode=True)
    return extract_json(comp.text) or {"verdict": "unknown", "findings": [], "summary": comp.text[:300]}
