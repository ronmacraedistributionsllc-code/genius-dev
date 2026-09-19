"""Dependency vulnerability scan via the ecosystem's own auditors (`npm audit`, `pip-audit`). Needs network access to the
public advisory databases — no API key. When an auditor is missing or offline the result says so instead of reporting "clean"."""
from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from .audit import Finding
from .project import Project
from .runner import CmdResult, run_command

RunFn = Callable[[str, Path, float], Awaitable[CmdResult]]
_SEV = {"critical": "high", "high": "high", "moderate": "medium", "medium": "medium", "low": "low", "info": "low"}


def _parse_npm(out: str) -> list[Finding]:
    data = json.loads(out)
    res: list[Finding] = []
    for name, v in (data.get("vulnerabilities") or {}).items():
        via = [x if isinstance(x, str) else x.get("title", "") for x in v.get("via", [])]
        fix = v.get("fixAvailable")
        note = "fix available" if fix else "no fix available"
        res.append(Finding("dependency", _SEV.get(v.get("severity", "low"), "low"), f"{name} ({v.get('range', '?')}): {'; '.join(x for x in via if x)[:100]} — {note}", "package.json", 0, bool(fix)))
    return res


def _parse_pip_audit(out: str) -> list[Finding]:
    data = json.loads(out)
    deps = data.get("dependencies", data) if isinstance(data, dict) else data
    res: list[Finding] = []
    for d in deps:
        for v in d.get("vulns", []):
            fixes = ", ".join(v.get("fix_versions", [])) or "no fix released"
            res.append(Finding("dependency", "high", f"{d['name']} {d.get('version', '?')}: {v.get('id', '?')} — fixed in {fixes}", "requirements", 0, bool(v.get("fix_versions"))))
    return res


async def dependency_findings(p: Project, run: RunFn | None = None) -> tuple[list[Finding], list[str]]:
    injected = run is not None
    run = run or (lambda c, cwd, t: run_command(c, cwd, t))
    findings: list[Finding] = []
    notes: list[str] = []
    root = p.root
    if (root / "package.json").exists():
        if not injected and not shutil.which("npm"):
            notes.append("npm audit: npm is not installed")
        else:
            r = await run("npm audit --json", root, 120)
            try:
                findings += _parse_npm(r.stdout)
                notes.append("npm audit: ran")
            except (json.JSONDecodeError, AttributeError):
                notes.append("npm audit: no usable output (offline, or no lockfile) — " + (r.output.strip().splitlines() or ["?"])[-1][:80])
    if "python" in p.info.languages:
        py = shlex.quote(p.info.python or "python3")
        r = await run(f"{py} -m pip_audit -f json --progress-spinner off", root, 300)
        if "No module named" in r.output:
            notes.append("pip-audit is not installed (pip install pip-audit) — Python dependencies were NOT scanned")
        else:
            try:
                findings += _parse_pip_audit(r.stdout)
                notes.append("pip-audit: ran")
            except (json.JSONDecodeError, KeyError, TypeError):
                notes.append("pip-audit: no usable output (offline?) — " + (r.output.strip().splitlines() or ["?"])[-1][:80])
    if not notes:
        notes.append("no supported dependency manifest (package.json / Python project) found")
    return findings, notes
