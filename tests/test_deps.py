import json

from genius_dev.deps import dependency_findings
from genius_dev.project import Project
from genius_dev.runner import CmdResult

NPM = {"vulnerabilities": {"lodash": {"severity": "high", "range": "<4.17.21", "via": [{"title": "Prototype Pollution"}], "fixAvailable": True},
                           "minimist": {"severity": "moderate", "range": "<1.2.6", "via": ["minimist"], "fixAvailable": False}}}
PIP = {"dependencies": [{"name": "requests", "version": "2.0.0", "vulns": [{"id": "PYSEC-1", "fix_versions": ["2.31.0"]}]}, {"name": "safe", "version": "1", "vulns": []}]}


def runner(script):
    async def run(cmd, cwd, timeout):
        for key, (code, out) in script.items():
            if key in cmd:
                return CmdResult(cmd, code, out, "", 0.1)
        return CmdResult(cmd, 0, "", "", 0.1)
    return run


async def test_npm_and_pip_audit_output_becomes_findings(tmp_path):
    (tmp_path / "package.json").write_text("{}"); (tmp_path / "app.py").write_text("x=1\n")
    p = Project.open(tmp_path)
    fs, notes = await dependency_findings(p, runner({"npm audit": (1, json.dumps(NPM)), "pip_audit": (1, json.dumps(PIP))}))
    by = {f.message.split()[0]: f for f in fs}
    assert by["lodash"].severity == "high" and by["lodash"].fixable and "Prototype Pollution" in by["lodash"].message
    assert by["minimist"].severity == "medium" and not by["minimist"].fixable and by["requests"].severity == "high" and "2.31.0" in by["requests"].message
    assert "safe" not in by and "npm audit: ran" in notes and "pip-audit: ran" in notes


async def test_missing_or_offline_auditors_are_reported_not_treated_as_clean(tmp_path, tmp_path_factory):
    (tmp_path / "package.json").write_text("{}"); (tmp_path / "app.py").write_text("x=1\n")
    p = Project.open(tmp_path)
    fs, notes = await dependency_findings(p, runner({"npm audit": (1, "npm ERR! request failed"), "pip_audit": (1, "No module named pip_audit")}))
    assert fs == [] and any("no usable output" in n for n in notes) and any("NOT scanned" in n for n in notes)
    empty = tmp_path_factory.mktemp("empty")
    assert (await dependency_findings(Project.open(empty), runner({})))[1] == ["no supported dependency manifest (package.json / Python project) found"]
