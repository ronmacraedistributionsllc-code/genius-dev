"""The mock model's deterministic scenarios drive the full loop: debugging, browser-verified UI work, and the demo command."""
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

import genius_dev
from genius_dev.cli import app
from genius_dev.providers.mock import scenario_for
from pathlib import Path

runner = CliRunner()
PKG = Path(genius_dev.__file__).parent


def fixture_dir(tmp_path, name):
    d = tmp_path / name
    shutil.copytree(PKG / name, d)
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    return d


async def test_debug_scenario_first_fix_fails_then_debugger_repairs_from_evidence(tmp_path, make_rt):
    rt = make_rt(fixture_dir(tmp_path, "demo_debug"))
    rep = await rt.agent.run("Fix slugify so all of its tests pass")
    assert rep.success and len(rt.project.reqs.all()) == 5 and all(r["status"] == "PASS" for r in rt.project.reqs.all())
    roles = [c["role"] for c in rt.router.provider("mock").calls]
    assert "debugger" in roles and roles.index("debugger") > roles.index("implementer")
    retry = [e for e in rt.bus.buffer if e.label == "RETRY"]
    assert retry and any("Diagnosis from the failing output" in e.message and "test_punctuation" in e.message for e in rt.bus.buffer if e.label == "STEP")
    assert "re.sub" in (rt.project.root / "slug.py").read_text()
    assert rep.files_changed == ["slug.py"] and rt.project.last_run("tests")["ok"] == 1


async def test_scenarios_cover_planning_requirements_checkpoint_resume_cost(tmp_path, make_rt):
    rt = make_rt(fixture_dir(tmp_path, "demo_debug"), {"mock": {"input_cost": 1.0, "output_cost": 4.0}})
    await rt.agent.run("Fix slugify so all of its tests pass")
    p = rt.project
    assert len(p.checkpoints.list()) == 2 and p.tasks.progress() == 100 and p.reqs.summary().complete            # checkpoint, tasks, requirements
    assert rt.gstore.usage_by_provider()[0]["cost"] > 0 and {r["key"] for r in rt.gstore.usage_grouped("role")} >= {"planner", "implementer", "debugger"}
    assert "PASS" in (p.gdir / "requirements.md").read_text() and "slugify" in (p.gdir / "architecture.md").read_text()      # memory + architecture
    from genius_dev.status import resume_view
    v = resume_view(rt)
    assert v["requirements"]["PASS"] == 5 and v["progress"] == 100 and v["tests"]["ok"]


def test_scenario_selection_is_deterministic_and_generic_fallback_is_honest():
    assert scenario_for("FILES calculator.py").name == "fix" and scenario_for("slug.py in tree").name == "debug"
    assert scenario_for("GOAL: Build a landing page for X").name == "web" and scenario_for("GOAL: something unrelated") is None


async def test_web_scenario_browser_audit_fails_then_repairs(tmp_path, make_rt):
    pytest.importorskip("playwright")
    from genius_dev.browser import browser_ready
    ok, why = await browser_ready()
    if not ok:
        pytest.skip(why)
    rt = make_rt(fixture_dir(tmp_path, "demo_web"))
    rep = await rt.agent.run("Build a landing page for Acme with a Get started button")
    assert rep.success, rep.text()
    reqs = {r["id"]: r for r in rt.project.reqs.all()}
    assert reqs["REQ-002"]["status"] == "PASS" and "audit clean" in reqs["REQ-002"]["result"]
    labels = [e.label for e in rt.bus.buffer]
    assert labels.count("AUDIT") >= 4 and "RETRY" in labels                       # audited (desktop+mobile) twice: fail, repair, pass
    assert any("horizontal scroll" in (e.detail or e.message) or "overflow" in e.message for e in rt.bus.buffer) or "RETRY" in labels
    assert "aria" in (rt.project.root / "index.html").read_text() and "Get started" in (rt.project.root / "index.html").read_text()
    assert rep.visual_qa.startswith("UI REVIEW") and "FAILURES 0" in rep.visual_qa
    assert not [p for p in rt.project.procs.list() if p["status"] == "running"]         # verification servers are cleaned up


@pytest.mark.parametrize("scenario,needle", [("fix", "Fix the failing tests"), ("debug", "Diagnose and repair")])
def test_demo_command_scenarios(scenario, needle):
    r = runner.invoke(app, ["demo", "--scenario", scenario, "--fast"])
    assert r.exit_code == 0 and "DONE" in r.output and needle in r.output and "Model cost tracked" in r.output
    for phase in ("Inspect the project", "Requirements & plan", "Create a checkpoint", "Inspect files", "Modify code", "Verify requirements"):
        assert phase in r.output
    assert "Update progress & finish" in r.output and "Before:" in r.output


def test_demo_rejects_unknown_scenario():
    assert runner.invoke(app, ["demo", "--scenario", "nope"]).exit_code == 2
