"""Permission modes, shell-command risk classification and protected paths."""
from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable

MODES = ("safe", "standard", "autonomous")

LOW, PROJECT, HIGH, BLOCKED = "low", "project", "high", "blocked"

_LOW_CMDS = {"ls", "cat", "grep", "rg", "find", "head", "tail", "wc", "pwd", "echo", "which", "diff", "tree", "stat", "file",
             "sort", "uniq", "cut", "date", "env", "printenv", "whoami", "uname", "sed", "awk", "test", "true", "false", "cd"}
_LOW_GIT = {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "blame", "remote", "describe", "tag", "stash"}
_TEST_RUNNERS = {"pytest", "unittest", "vitest", "jest", "mocha", "tox", "ruff", "flake8", "mypy", "pyright", "eslint", "tsc", "prettier",
                 "black", "isort", "playwright", "cypress"}
_PKG = {"npm", "pnpm", "yarn", "pip", "pip3", "uv", "poetry", "bundle", "composer", "cargo", "go", "gradle", "gradlew", "make",
        "brew", "bun", "flutter", "dart", "npx", "pipx", "swift", "xcodebuild", "python", "python3", "node", "deno"}
_HIGH = {"rm", "rmdir", "sudo", "dd", "mkfs", "diskutil", "chmod", "chown", "launchctl", "kill", "killall", "pkill", "shutdown",
         "reboot", "mv", "ssh", "scp", "rsync", "curl", "wget", "nc", "systemsetup", "defaults", "csrutil", "aws", "gcloud", "az",
         "kubectl", "terraform", "docker", "fly", "vercel", "netlify", "heroku", "firebase", "supabase", "psql", "mysql", "sh", "bash", "zsh"}
_BLOCK_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*\s+)*(/|~|\$HOME|/\*|\.\.?)(\s|$)", r"\bsudo\b", r":\(\)\s*\{", r"\bmkfs\b", r"\bdd\s+.*of=/dev/",
    r">\s*/dev/(sd|disk)", r"\bchmod\s+-R\s+777\s+/", r"\bgit\s+push\b.*--force", r"\bcurl\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh)",
    r"\bwget\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh)",
]
_DESTRUCTIVE_GIT = re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|push|checkout\s+--|rebase|branch\s+-D)")
_SAFE_DEV_RM = re.compile(r"^rm\s+(-[rf]+\s+)?(\./)?(node_modules|dist|build|__pycache__|\.pytest_cache|\.next|target)/?$")
_MIGRATE = re.compile(r"\b(migrate|db\s+push|prisma\s+migrate|alembic\s+upgrade)\b")
_PROD = re.compile(r"(?i)\b(prod|production)\b")


@dataclass
class Decision:
    action: str        # allow | ask | deny
    reason: str
    risk: str = LOW


def _split_segments(cmd: str) -> list[list[str]] | None:
    """Quote-aware split of a command line into simple commands (on ; && || | & and newlines). None if it cannot be parsed."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return None
    segs: list[list[str]] = [[]]
    for t in toks:
        if t and set(t) <= set(";&|") :
            segs.append([])
        else:
            segs[-1].append(t)
    return [s for s in segs if s]


def classify_shell(cmd: str) -> tuple[str, str]:
    """Return (risk, reason). Pipelines/&&-chains take the highest risk of any segment."""
    c = cmd.strip()
    if not c:
        return LOW, "empty"
    for pat in _BLOCK_PATTERNS:
        if re.search(pat, c):
            return BLOCKED, "matches a destructive/system-level pattern"
    if "$(" in c or "`" in c:
        return HIGH, "command substitution hides what will run"
    segs = _split_segments(c)
    if segs is None:
        return HIGH, "unparseable shell syntax"
    worst, why = LOW, "read-only or test/lint command"
    order = {LOW: 0, PROJECT: 1, HIGH: 2, BLOCKED: 3}
    for seg in segs:
        r, w = _classify_segment(seg)
        if order[r] > order[worst]:
            worst, why = r, w
    redirects = any(t in (">", ">>") or t.startswith(">") for seg in segs for t in seg)
    if redirects and worst == LOW and not re.search(r">\s*/dev/null|2>&1", c):
        worst, why = PROJECT, "redirects output to a file"
    return worst, why


def _classify_segment(toks: list[str]) -> tuple[str, str]:
    seg = " ".join(shlex.quote(t) if t == "" else t for t in toks)
    toks = [t for t in toks if t not in (">", ">>", "<", "2>", "&>") and not (t.startswith(">") or t.startswith("2>"))] if any(x in toks for x in (">", ">>", "2>")) else toks
    toks = list(toks)
    while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
        toks.pop(0)                                    # leading env assignments
    if not toks:
        return LOW, "assignment"
    exe = Path(toks[0]).name
    rest = toks[1:]
    if _SAFE_DEV_RM.match(seg):
        return PROJECT, "removes a build artefact directory"
    if _DESTRUCTIVE_GIT.search(seg):
        return HIGH, "destructive or publishing git operation"
    if exe == "git":
        sub = next((t for t in rest if not t.startswith("-")), "")
        return (LOW, "read-only git") if sub in _LOW_GIT else (PROJECT, f"git {sub}")
    if exe in _HIGH:
        return HIGH, f"`{exe}` can be destructive, system-level or external"
    if exe == "sed" and any(a.startswith("-i") for a in rest):
        return PROJECT, "in-place edit"
    if exe in _LOW_CMDS:
        return LOW, "read-only command"
    if exe in _TEST_RUNNERS:
        return LOW, "test/lint/format tool"
    if exe in _PKG:
        sub = " ".join(rest[:3])
        if _MIGRATE.search(seg) or _PROD.search(seg):
            return HIGH, "database migration or production target"
        if re.search(r"\b(publish|deploy|login|upload)\b", sub):
            return HIGH, "publishes or authenticates externally"
        if re.search(r"\b(install|add|remove|uninstall|update|upgrade|init|create|sync|new)\b", sub):
            return PROJECT, "modifies dependencies"
        if re.search(r"\b(test|run|build|check|lint|typecheck|dev|start|-m|-c|--version|-v)\b", sub) or exe in ("python", "python3", "node"):
            return LOW, "build/test/run"
        return PROJECT, "package/build tool"
    if exe in ("mkdir", "touch", "cp", "ln", "tee", "patch", "open"):
        return PROJECT, "creates or copies files"
    return HIGH, f"unrecognised command `{exe}`"


AskFn = Callable[[str, str], Awaitable[bool]]


class Permissions:
    def __init__(self, mode: str, root: Path, protected: list[str] | None = None, ask: AskFn | None = None):
        assert mode in MODES, mode
        self.mode = mode
        self.root = root.resolve()
        self.protected = list(protected or [])
        self.overrides: set[str] = set()     # protected paths the user explicitly unlocked this session
        self.ask = ask
        self.skip_rules: list[str] = []      # "don't touch the frontend" style dynamic globs

    # -- paths ------------------------------------------------------------------
    def inside_project(self, p: Path) -> bool:
        try:
            p.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def is_protected(self, rel: str) -> bool:
        rel = rel.strip("/")
        for pat in self.protected + self.skip_rules:
            pat = pat.strip("/")
            if rel == pat or rel.startswith(pat + "/") or fnmatch.fnmatch(rel, pat):
                if pat not in self.overrides:
                    return True
        return False

    # -- decisions --------------------------------------------------------------
    def check_write(self, rel: str, kind: str = "write", size_hint: int = 0) -> Decision:
        p = (self.root / rel)
        if not self.inside_project(p):
            return Decision("ask" if self.mode != "safe" else "ask", f"{rel} is outside the project directory", HIGH)
        if self.is_protected(rel):
            return Decision("ask", f"{rel} is protected", HIGH)
        if kind == "delete" and size_hint > 50:
            return Decision("ask", f"deleting {size_hint} files", HIGH)
        if self.mode == "safe":
            return Decision("ask", f"SAFE mode: confirm {kind} {rel}", PROJECT)
        return Decision("allow", f"{kind} inside project", PROJECT)

    def check_shell(self, cmd: str, explicit_external: bool = False) -> Decision:
        risk, why = classify_shell(cmd)
        if risk == BLOCKED:
            return Decision("deny", f"blocked: {why}", BLOCKED)
        if self.mode == "safe":
            return Decision("ask", f"SAFE mode: confirm shell ({why})", risk)
        if risk == HIGH:
            return Decision("ask", why, HIGH)                # even AUTONOMOUS asks
        if risk == PROJECT and self.mode == "standard" and re.search(r"\b(install|add|update|upgrade|remove|uninstall)\b", cmd):
            return Decision("allow", why, risk)              # dependency changes are project-scoped, allowed in standard
        return Decision("allow", why, risk)

    async def resolve(self, d: Decision, title: str) -> bool:
        """Turn an 'ask' decision into a boolean using the UI callback (headless: deny)."""
        if d.action == "allow":
            return True
        if d.action == "deny":
            return False
        if self.ask is None:
            return False
        return await self.ask(title, d.reason)
