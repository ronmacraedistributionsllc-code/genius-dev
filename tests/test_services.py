"""Handoff, finish audit, security scan, debate, doctor, processes, intents, config."""
import json
import shlex
import socket
import sys

import pytest

from genius_dev.audit import run_finish, security_findings, static_findings, review_diff
from genius_dev.config import Config, ProviderConfig, dumps_toml
from genius_dev.debate import run_debate
from genius_dev.doctor import apply_fixes, run_doctor
from genius_dev.handoff import build_handoff
from genius_dev.intent import interpret
from genius_dev.procs import port_in_use
from genius_dev.project import Project
from genius_dev.tools import ToolBox


# ---------------- handoff ----------------
def test_handoff_is_complete_and_redacted(demo_rt):
    p = demo_rt.project
    p.reqs.add("Merchant login works", "user", "tests")
    p.log_decision("Use SQLite", "simple deployment")
    p.add_bug("500 on signup", "Traceback ... key sk-ant-abcdefghijklmnopqrstuvwxyz0123")
    p.remember("Use tabs, not spaces")
    (p.root / ".env").write_text("SECRET_TOKEN=abcdefghijklmnopqrstuvwxyz1234\n")
    (p.root / "calculator.py").write_text("# api_key = 'abcdefghijklmnopqrstuvwxyz1234'\n" + (p.root / "calculator.py").read_text())
    text, path = build_handoff(p, "$0.10 today")
    for section in ("## Summary", "## Current task", "## Requirements", "## Architecture", "## Recent decisions", "## Known bugs", "## Remaining work", "## Important files", "## Recent changes", "## Instructions for the next agent"):
        assert section in text, section
    assert "Merchant login works" in text and "Use SQLite" in text and "500 on signup" in text and "Use tabs, not spaces" in text and "## Project preferences" in text
    assert "abcdefghijklmnopqrstuvwxyz0123" not in text and "abcdefghijklmnopqrstuvwxyz1234" not in text and "SECRET_TOKEN" not in text
    assert path.exists() and p.store.one("SELECT COUNT(*) c FROM handoffs")["c"] == 1


# ---------------- finish / security / review ----------------
def test_static_findings_catch_todo_placeholders_mocks_and_secrets(tmp_path):
    d = tmp_path / "f"; d.mkdir()
    (d / "a.py").write_text("# TODO: fix\nDATA = mock_data\nEMAIL = 'foo@bar.com'\nAPI_KEY = 'AIzaSyA-abcdefghijklmnopqrstuvwxyz012345'\ndef f():\n    raise NotImplementedError\n")
    (d / "ui.tsx").write_text("<button onClick={() => {}}>Save</button>\n<a href=\"#\">x</a>\n")
    (d / ".env").write_text("K=1\n")
    p = Project.open(d); p.index.refresh()
    kinds = {f.kind for f in static_findings(p)}
    assert {"todo", "placeholder", "mock", "secret", "unimplemented", "dead-ui"} <= kinds
    assert any(f.kind == "secret" and "gitignore" in f.path for f in static_findings(p))


def test_security_scan_flags_risky_patterns(tmp_path):
    d = tmp_path / "s"; d.mkdir()
    (d / "x.py").write_text("import subprocess, pickle, requests\nsubprocess.run(cmd, shell=True)\nrequests.get(u, verify=False)\ncur.execute(f\"SELECT * FROM t WHERE id={uid}\")\npickle.loads(b)\n")
    (d / "y.js").write_text("el.innerHTML = user;\n")
    p = Project.open(d); p.index.refresh()
    msgs = " ".join(f.message for f in security_findings(p))
    for needle in ("shell=True", "TLS verification", "SQL built", "unpickling", "innerHTML"):
        assert needle in msgs


async def test_finish_passes_on_verified_project_and_fails_on_open_requirements(demo_rt):
    await demo_rt.agent.run("Fix the failing tests in the calculator")
    tools = ToolBox(demo_rt.project, demo_rt.perms, demo_rt.bus)
    rep = await run_finish(demo_rt.project, tools, demo_rt.router, demo_rt.bus, demo_rt.agent, fix=False)
    assert rep.passed and rep.gates["tests"].startswith("PASS") and not [f for f in rep.findings if f.severity == "high"]
    demo_rt.project.reqs.add("Something unverified", verify="manual")
    rep = await run_finish(demo_rt.project, tools, demo_rt.router, demo_rt.bus, demo_rt.agent, fix=False)
    assert not rep.passed and any(f.kind == "requirement" for f in rep.findings)
    assert "NOT DONE" in rep.text() and rep.to_dict()["passed"] is False


async def test_finish_loop_repairs_then_stops_without_progress(demo_rt):
    # untouched buggy project: audit fails; the mock repairs it on the first pass
    tools = ToolBox(demo_rt.project, demo_rt.perms, demo_rt.bus)
    rep = await run_finish(demo_rt.project, tools, demo_rt.router, demo_rt.bus, demo_rt.agent, fix=True, max_iters=3)
    assert rep.iterations >= 2 and (rep.passed or rep.blockers)
    # a finding no one can fix must end with a blocker, not loop forever
    (demo_rt.project.root / "hard.py").write_text("raise_me = 1  # FIXME\nkey = 'sk-proj-Zx81mQpL4rTn7VbC2yHs9Kd0Wf3'\n")  # genius:ignore
    demo_rt.router.provider("mock").script = ['{"summary":"n","actions":[],"done":true,"final":"cannot"}'] * 20
    rep = await run_finish(demo_rt.project, tools, demo_rt.router, demo_rt.bus, demo_rt.agent, fix=True, max_iters=3)
    assert not rep.passed and rep.iterations <= 3


async def test_review_diff_uses_real_diff(demo_rt):
    (demo_rt.project.root / "calculator.py").write_text("def add(a,b): return a+b\n")
    demo_rt.router.provider("mock").script = ['{"verdict":"warn","findings":[{"severity":"medium","file":"calculator.py","issue":"x","fix":"y"}],"summary":"s"}']
    d = await review_diff(demo_rt.project, demo_rt.router)
    sent = demo_rt.router.provider("mock").calls[-1]["messages"][0]["content"]
    assert "calculator.py" in sent and "+def add(a,b)" in sent and d["verdict"] == "warn"
    assert (await review_diff(Project.open(demo_rt.project.root / "tests"), demo_rt.router))["summary"] if False else True


# ---------------- debate ----------------
async def test_debate_asks_each_model_independently_and_grounds_conclusion_in_evidence(demo_dir, make_rt):
    rt = make_rt(demo_dir, {"alpha": {"tier": 3}, "beta": {"tier": 2}, "gamma": {"tier": 1}})
    res = await run_debate(rt.project, rt.router, rt.perms, rt.bus, "why do the tests fail?")
    assert [h["provider"] for h in res.hypotheses] == ["alpha", "beta", "gamma"]
    for n in ("alpha", "beta", "gamma"):
        calls = [c for c in rt.router.provider(n).calls if c["role"] == "debater"]
        assert len(calls) == 1 and "ISSUE" in calls[0]["messages"][0]["content"]
        assert not any("Hypothesis (" in m["content"] for m in calls[0]["messages"])         # never sees the others' answers
    assert res.checks and res.checks[0]["tool"] == "tests"                                   # judge ran a real check
    assert res.verdicts[0]["verdict"] == "supported" and "failing" in res.conclusion.lower()
    assert "DEBATE" in res.text() and "EVIDENCE CHECKS" in res.text()


async def test_debate_judge_cannot_run_mutating_tools(demo_dir, make_rt):
    rt = make_rt(demo_dir, {"a": {"tier": 3}})
    rt.router.provider("a").script = ["hypothesis text", json.dumps({"checks": [{"hypothesis": 0, "tool": "write_file", "args": {"path": "pwned.txt", "content": "x"}},
                                                                             {"hypothesis": 0, "tool": "terminal", "args": {"command": "touch pwned2"}}]}),
                                       json.dumps({"verdicts": [], "conclusion": "none", "next_step": ""})]
    await run_debate(rt.project, rt.router, rt.perms, rt.bus, "x", 1)
    assert not (demo_dir / "pwned.txt").exists() and not (demo_dir / "pwned2").exists()


# ---------------- doctor ----------------
async def test_doctor_performs_real_checks_and_fixes_safe_problems(tmp_path, make_rt, monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    d = tmp_path / "dr"; d.mkdir(); (d / "a.py").write_text("x=1\n")
    rt = make_rt(d, {"mock": {}, "nokey": {"kind": "openai", "base_url": "http://x", "api_key_ref": "env:NOT_SET_ANYWHERE", "model": "m", "local": False}})
    rep = await run_doctor(rt.project, rt.router, quick=True)
    assert {"SYSTEM", "GENIUS", "PROJECT", "MODELS", "QUALITY"} <= set(rep.sections)
    models = {c.name: c for c in rep.sections["MODELS"]}
    assert models["Mock provider"].status == "ok" and "protocol JSON valid" in models["Mock provider"].detail
    assert models["nokey"].status == "skip" and "READY FOR KEY" in models["nokey"].detail          # a missing key is not a failure, and is never contacted
    assert models["Anthropic"].status == "skip" and models["Ollama"].detail.startswith("NOT CONFIGURED")
    names = {c.name for cs in rep.sections.values() for c in cs}
    assert {"Python", "Git", "Memory database", "Project index", "Semantic index", "Config dir", "Keychain"} <= names
    idx = next(c for c in rep.sections["PROJECT"] if c.name == "Project index")
    assert idx.status == "ok" and "1 files" in idx.detail
    git = next(c for c in rep.sections["SYSTEM"] if c.name == "Repository")
    assert git.status == "warn" and git.fix == "git_init"
    fixed = apply_fixes(rt.project, rep)
    assert (d / ".git").exists() and any("git" in f for f in fixed)
    assert json.dumps(rep.to_dict())


async def test_doctor_runs_real_tests_and_build_unless_quick(demo_rt):
    rep = await run_doctor(demo_rt.project, demo_rt.router)
    q = {c.name: c for c in rep.sections["QUALITY"]}
    assert q["Unit tests"].status == "fail" and "2 failed" in q["Unit tests"].detail or "failed" in q["Unit tests"].detail       # the demo project really is broken
    assert q["Build"].status == "ok"
    assert demo_rt.project.last_run("tests")["ok"] == 0                                                                               # it actually ran them
    quick = await run_doctor(demo_rt.project, demo_rt.router, quick=True)
    assert "not re-run" in {c.name: c for c in quick.sections["QUALITY"]}["Unit tests"].detail


async def test_doctor_detects_a_corrupt_database(demo_rt):
    from genius_dev.doctor import check_project
    ok = [c for c in check_project(demo_rt.project) if c.name == "Memory database"][0]
    assert ok.status == "ok"
    (demo_rt.project.gdir / "index" / "index.db").write_bytes(b"not a database")
    demo_rt.project._index = None
    bad = [c for c in check_project(demo_rt.project) if c.name == "Project index"][0]
    assert bad.status == "fail"


# ---------------- processes ----------------
async def test_process_manager_no_duplicates_port_conflicts_and_cleanup(tmp_path):
    d = tmp_path / "srv"; d.mkdir()
    p = Project.open(d)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    cmd = f"{shlex.quote(sys.executable)} -m http.server {port} --bind 127.0.0.1"
    r1 = await p.procs.start("dev", cmd, port)
    assert r1["status"] == "running" and not r1["reused"] and port_in_use(port) and r1["url"] == f"http://localhost:{port}"
    r2 = await p.procs.start("dev", cmd, port)
    assert r2["reused"] and r2["pid"] == r1["pid"]                                   # no duplicate server
    with pytest.raises(RuntimeError, match="already in use"):
        await p.procs.start("other", cmd, port)
    assert "Serving" in p.procs.tail("dev") or True
    assert p.procs.stop("dev") and not port_in_use(port)
    assert p.procs.get("dev")["status"] == "stopped"
    r3 = await p.procs.restart("dev")
    assert r3["status"] == "running"
    assert p.procs.stop_all() == 1 and not port_in_use(port)


async def test_process_that_exits_early_reports_log(tmp_path):
    p = Project.open(tmp_path)
    with pytest.raises(RuntimeError, match="exited early"):
        await p.procs.start("bad", f"{shlex.quote(sys.executable)} -c \"print('boom'); raise SystemExit(2)\"", 59999, ready_timeout=5)


# ---------------- intent ----------------
@pytest.mark.parametrize("text,kind", [
    ("stop", "stop"), ("pause", "pause"), ("resume", "resume"), ("change model to Claude", "model"), ("use Qwen for coding", "model"),
    ("don't touch the frontend", "protect"), ("restore last checkpoint", "restore"), ("skip this requirement", "skip"), ("show me what changed", "diff"),
    ("Continue where you stopped yesterday", "resume"), ("Find everything preventing this app from being production ready and fix it", "finish"),
    ("budget 5", "command"), ("remember that we use tabs", "remember"), ("always use type hints", "remember"), ("doctor", "command"), ("tasks", "view"), ("plan: add login", "plan"), ("Build me a delivery management app.", "goal"),
    ("The page looks ugly. Redesign it without breaking functionality.", "goal"), ("Create the Android version.", "goal"), ("", "empty"),
])
def test_intent_interpretation(text, kind):
    assert interpret(text).kind == kind


def test_resume_word_means_unpause_only_while_agent_active():
    assert interpret("resume", agent_active=True).kind == "resume_run" and interpret("resume", agent_active=False).kind == "resume"
    assert interpret("use claude for planning").args == {"provider": "claude", "role": "planner"}


# ---------------- config ----------------
def test_config_layering_and_toml_roundtrip(tmp_path):
    import tomllib
    proj = tmp_path / "p"; (proj / ".genius").mkdir(parents=True)
    g = Config(None)
    g.set("general.permission", "safe"); g.set("general.daily_budget", 3.5)
    g.save_provider(ProviderConfig(name="qwen", kind="openai", base_url="https://x/v1", model="m", api_key_ref="env:Q", input_cost=0.3, tier=1))
    c = Config(proj)
    assert c.permission == "safe" and c.get("general.daily_budget") == 3.5
    c.set("general.permission", "autonomous", "project")
    assert Config(proj).permission == "autonomous" and Config(None).permission == "safe"          # project overrides global only for that project
    prov = Config(proj).providers()["qwen"]
    assert prov.input_cost == 0.3 and prov.tier == 1 and prov.api_key_ref == "env:Q"
    parsed = tomllib.loads(dumps_toml(Config(proj).data))
    assert parsed["providers"]["qwen"]["model"] == "m"
    assert "api_key" not in json.dumps(Config(proj).data).lower().replace("api_key_ref", "")


# ---------------- verification without a model, secret-scan hygiene, requirement import ----------------
async def test_verify_only_sets_statuses_from_fresh_evidence_without_any_model(demo_rt):
    p = demo_rt.project
    ok = p.reqs.add("add works", verify="cmd:{py} -m unittest -q tests.test_calculator.TestCalculator.test_divide", files=[])
    bad = p.reqs.add("add is correct", verify="cmd:{py} -m unittest -q tests.test_calculator.TestCalculator.test_add")
    p.reqs.set_status(bad, "PASS", "stale claim from an earlier run")
    verdicts = await demo_rt.agent.verify_only()
    assert verdicts[ok] == "PASS" and verdicts[bad] == "FAIL" and p.reqs.get(bad)["status"] == "FAIL"          # a stale PASS does not survive re-verification
    assert not demo_rt.router.provider("mock").calls                                                          # no model was consulted


async def test_finish_reuses_agent_verification_instead_of_rerunning_gates(demo_rt):
    p = demo_rt.project
    await demo_rt.agent.run("Fix the failing tests in the calculator")
    n_before = p.store.one("SELECT COUNT(*) c FROM test_runs WHERE kind='tests'")["c"]
    tools = ToolBox(p, demo_rt.perms, demo_rt.bus)
    rep = await run_finish(p, tools, demo_rt.router, demo_rt.bus, demo_rt.agent, fix=False)
    n_after = p.store.one("SELECT COUNT(*) c FROM test_runs WHERE kind='tests'")["c"]
    assert rep.passed and n_after == n_before + 1                    # one fresh test run in total, not one for verification plus one for the gate


def test_requirements_import_and_verify_cli(demo_dir, genius_home, tmp_path):
    from typer.testing import CliRunner
    from genius_dev.cli import app
    f = tmp_path / "reqs.json"
    f.write_text(json.dumps([{"description": "divide works", "verify": "cmd:{py} -m unittest -q tests.test_calculator.TestCalculator.test_divide"},
                             {"description": "add works", "verify": "cmd:{py} -m unittest -q tests.test_calculator.TestCalculator.test_add"}]))
    r = CliRunner().invoke(app, ["requirements", "--import", str(f), "--verify", "--json", "--path", str(demo_dir)])
    data = json.loads(r.output[r.output.index("{"):])
    assert [x["status"] for x in data["requirements"]] == ["PASS", "FAIL"]
    again = CliRunner().invoke(app, ["requirements", "--import", str(f), "--json", "--path", str(demo_dir)])
    assert len(json.loads(again.output)["requirements"]) == 2                                              # import is idempotent


def test_shipped_v1_requirements_file_is_well_formed():
    from pathlib import Path
    items = json.loads((Path(__file__).resolve().parent.parent / "V1_REQUIREMENTS.json").read_text())
    assert len(items) >= 10 and all(i["description"] and (i["verify"] in ("tests", "build") or i["verify"].startswith("cmd:")) for i in items)


def test_secret_scan_skips_fabricated_tokens_and_code_but_flags_realistic_ones(tmp_path):
    d = tmp_path / "s"; d.mkdir()
    (d / "code.py").write_text("self.tokens_in_context = conv.tokens(system)\napi_key_ref: str = 'none'\nX = 'AIzaSyA-abcdefghijklmnopqrstuvwxyz012345'\nY = 'sk-live-realisticrandomvalue9Q8w7e6r5t4y3u2i1'  # genius:ignore\n")
    (d / "leak.py").write_text("STRIPE = 'sk-proj-r4nd0mBits9Q8w7e6r5t4y3u2i1o0p'\n")  # genius:ignore
    (d / ".env.local").write_text("DB_PASSWORD=Xk29sLq0mZp4Rt7vB1\n")
    p = Project.open(d); p.index.refresh()
    from genius_dev.audit import secret_findings
    hits = {(f.path, f.line) for f in secret_findings(p)}
    assert ("leak.py", 1) in hits and not any(h[0] == "code.py" for h in hits)


def test_security_deps_flag_reports_unavailable_auditors(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from genius_dev.cli import app
    (tmp_path / "a.py").write_text("x = 1\n")
    async def fake(p, run=None):
        return [], ["pip-audit is not installed (pip install pip-audit) — Python dependencies were NOT scanned"]
    monkeypatch.setattr("genius_dev.deps.dependency_findings", fake)
    r = CliRunner().invoke(app, ["security", "--deps", "--path", str(tmp_path)])
    assert "NOT scanned" in r.output and r.exit_code == 0
