"""Subprocess execution with timeouts, process-group cleanup, and test/build output parsing."""
from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from .secrets import redact


@dataclass
class CmdResult:
    command: str
    code: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return (self.stdout + ("\n" + self.stderr if self.stderr.strip() else "")).strip()


async def run_command(cmd: str, cwd: Path, timeout: float = 300, env: dict[str, str] | None = None,
                      max_bytes: int = 2_000_000) -> CmdResult:
    t = time.perf_counter()
    full_env = {**os.environ, "CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0", **(env or {})}
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=str(cwd), env=full_env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_group(proc.pid)
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        _kill_group(proc.pid)
        raise
    return CmdResult(cmd, proc.returncode if proc.returncode is not None else -1,
                     redact(out[:max_bytes].decode(errors="replace")), redact(err[:max_bytes].decode(errors="replace")),
                     time.perf_counter() - t, timed_out)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def truncate(text: str, limit: int = 6000, keep_tail: bool = True) -> str:
    """Shrink logs for model context: keep head and tail (errors are usually at the end)."""
    if len(text) <= limit:
        return text
    head, tail = (limit // 3, limit - limit // 3 - 40) if keep_tail else (limit - 40, 0)
    cut = len(text) - head - tail
    return text[:head] + f"\n… [{cut} chars omitted] …\n" + (text[-tail:] if tail else "")


@dataclass
class TestSummary:
    passed: int = 0
    failed: int = 0
    total: int = 0
    failures: list[str] = field(default_factory=list)
    ok: bool = False
    framework: str = ""

    def line(self) -> str:
        return f"{self.passed}/{self.total} passed" + (f", {self.failed} failed" if self.failed else "")


def parse_tests(framework: str, res: CmdResult) -> TestSummary:
    out = res.output
    s = TestSummary(framework=framework)
    fw = framework.lower()
    if "gradle" in fw:
        m = re.search(r"(\d+) tests? completed(?:, (\d+) failed)?", out)
        s.total = int(m.group(1)) if m else 0
        s.failed = int(m.group(2) or 0) if m else (0 if res.ok else 1)
        s.passed = max(0, s.total - s.failed) if m else int(res.ok)
        s.total = s.total or s.passed + s.failed
        s.failures = re.findall(r"^(\S+ > \S+.*) FAILED$", out, re.M)[:20]
    elif "flutter" in fw:
        ms = re.findall(r"\+(\d+)(?: ~\d+)?(?: -(\d+))?:", out)
        s.passed = int(ms[-1][0]) if ms else 0
        s.failed = int(ms[-1][1] or 0) if ms else int(not res.ok)
        s.total = s.passed + s.failed
        s.failures = re.findall(r"\[E\]\s*(.+)", out)[:20]
    elif "xctest" in fw or "swift" in fw or "xcodebuild" in fw:
        ex = re.findall(r"Executed (\d+) tests?, with (\d+) failures?", out)
        s.total = int(ex[-1][0]) if ex else 0
        s.failed = int(ex[-1][1]) if ex else int(not res.ok)
        s.passed = max(0, s.total - s.failed)
        s.failures = re.findall(r"Test Case '(.+?)' failed", out)[:20]
    elif "playwright" in fw:
        f = re.search(r"(\d+) failed", out); p_ = re.search(r"(\d+) passed", out)
        s.failed, s.passed = int(f.group(1)) if f else 0, int(p_.group(1)) if p_ else 0
        s.total = s.passed + s.failed
        s.failures = [l.strip() for l in out.splitlines() if re.match(r"\s*\d+\) ", l)][:20]
    elif "cypress" in fw:
        t = re.search(r"Tests:\s+(\d+)\s+.*?Passing:\s+(\d+)\s+.*?Failing:\s+(\d+)", out)
        if t:
            s.total, s.passed, s.failed = int(t.group(1)), int(t.group(2)), int(t.group(3))
        else:
            s.total, s.passed, s.failed = 1, int(res.ok), int(not res.ok)
    elif "unittest" in fw:
        m = re.search(r"Ran (\d+) tests?", out)
        total = int(m.group(1)) if m else 0
        fm = re.search(r"FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?", out)
        s.failed = (int(fm.group(1) or 0) + int(fm.group(2) or 0)) if fm else 0
        s.total, s.passed = total, max(0, total - s.failed)
        s.failures = [l.split(": ", 1)[1].strip() for l in out.splitlines() if l.startswith(("FAIL: ", "ERROR: "))][:20]
        if not m and not res.ok:
            s.total, s.failed = 1, 1
    elif "pytest" in fw or re.search(r"=+ .*(passed|failed|error).* in [\d.]+s", out) or re.search(r"\d+ (passed|failed)", out) and "pytest" in out.lower():
        counts = {k: 0 for k in ("passed", "failed", "error", "errors", "skipped")}
        found = re.findall(r"(\d+) (passed|failed|errors?|skipped)", out.splitlines()[-1] if out else "")
        if not found:
            found = re.findall(r"(\d+) (passed|failed|errors?|skipped)", "\n".join(out.splitlines()[-5:]))
        for n, k in found:
            counts[k] += int(n)
        s.passed = counts["passed"]
        s.failed = counts["failed"] + counts["error"] + counts["errors"]
        s.total = s.passed + s.failed
        s.failures = [l.strip()[7:].strip() for l in out.splitlines() if l.startswith("FAILED ") or l.startswith("ERROR ")][:20]
        if "no tests ran" in out:
            s.total = 0
    elif "vitest" in fw or "jest" in fw or "npm" in fw or "yarn" in fw or "pnpm" in fw:
        m = re.search(r"Tests:?\s+(?:(\d+) failed[,|] ?)?(?:(\d+) skipped[,|] ?)?(?:(\d+) passed)?[^\n]*?(\d+)?", out)
        f = re.search(r"(\d+) failed", out); p = re.search(r"(\d+) passed", out); t = re.search(r"(\d+) total", out)
        s.failed = int(f.group(1)) if f else 0
        s.passed = int(p.group(1)) if p else 0
        s.total = int(t.group(1)) if t else s.passed + s.failed
        s.failures = [l.strip() for l in out.splitlines() if re.match(r"\s*(✕|×|FAIL)\s", l)][:20]
    elif "cargo" in fw:
        for a, b in re.findall(r"(\d+) passed; (\d+) failed", out):
            s.passed += int(a); s.failed += int(b)
        s.total = s.passed + s.failed
        s.failures = [l.strip() for l in out.splitlines() if l.strip().endswith("FAILED")][:20]
    elif "go" in fw:
        s.passed = len(re.findall(r"^(?:ok)\s", out, re.M))
        fails = re.findall(r"^--- FAIL: (\S+)", out, re.M) + re.findall(r"^FAIL\s+(\S+)", out, re.M)
        s.failed, s.failures = len(fails), fails[:20]
        s.total = s.passed + s.failed
    else:
        s.total, s.passed, s.failed = 1, int(res.ok), int(not res.ok)
    s.ok = res.ok and s.failed == 0
    return s


# Error classification for the self-healing loop --------------------------------------------------
_CLASSES = [
    ("dependency_missing", r"(ModuleNotFoundError|No module named|Cannot find module|command not found|ImportError: cannot import|could not find|package .* not found)"),
    ("port_conflict", r"(EADDRINUSE|address already in use|port \d+ is (already )?in use)"),
    ("permission_denied", r"(Permission denied|EACCES|Operation not permitted)"),
    ("syntax_error", r"(SyntaxError|IndentationError|Unexpected token|error TS1\d\d\d|parse error|expected .* found)"),
    ("type_error", r"(error TS\d+|TypeError|mypy|type mismatch)"),
    ("rate_limit", r"(429|rate limit|Too Many Requests)"),
    ("authentication", r"(401|403|Unauthorized|authentication failed|invalid api key)"),
    ("tool_unavailable", r"(not found: |No such file or directory.*(bin|node|python)|is not recognized)"),
    ("test_failure", r"(FAILED|AssertionError|assert |✕|tests? failed|failures?:)"),
]


def classify_failure(text: str) -> str:
    for name, pat in _CLASSES:
        if re.search(pat, text, re.I):
            return name
    return "unknown"
