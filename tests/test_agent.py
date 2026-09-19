"""Integration tests: full autonomous loop against the deterministic mock model."""
import asyncio
import json

import pytest

from genius_dev.controller import Controller
from genius_dev.providers import ProviderError
from genius_dev.runtime import recover_crashed_state


def J(**kw):
    return json.dumps(kw)


PLAN_TESTS = J(requirements=[{"description": "greet says hi", "verify": "tests"}], tasks=[{"title": "Fix greet", "reqs": [0]}])


def act(summary, *actions, done=False, final=""):
    return J(summary=summary, actions=[{"tool": t, "args": a} for t, a in actions], done=done, final=final)


@pytest.fixture
def rt(py_project, make_rt):
    (py_project / "app.py").write_text("def greet(name):\n    return 'bye ' + name\n")
    return make_rt(py_project)


async def test_demo_end_to_end_fix(demo_rt):
    rep = await demo_rt.agent.run("Fix the failing tests in the calculator")
    assert rep.success and rep.status == "complete" and rep.files_changed == ["calculator.py"]
    reqs = demo_rt.project.reqs.all()
    assert [r["status"] for r in reqs] == ["PASS"] * 3
    assert "a + b" in (demo_rt.project.root / "calculator.py").read_text()
    assert all(t["status"] == "done" for _, t in demo_rt.project.tasks.flat())
    assert rep.checkpoint.startswith("cp-0001") and "undo" in rep.checkpoint
    assert "4/4" in rep.test_results and rep.how_to_run
    assert "DONE" in rep.text() and "VERIFIED" in rep.text()
    # memory was updated
    assert "PASS" in (demo_rt.project.gdir / "requirements.md").read_text() and "complete" in (demo_rt.project.gdir / "progress.md").read_text().lower()
    assert demo_rt.project.last_session()["status"] == "complete"


async def test_undo_after_run_restores_original_bug(demo_rt):
    await demo_rt.agent.run("Fix the failing tests in the calculator")
    demo_rt.project.checkpoints.undo()
    assert "a - b" in (demo_rt.project.root / "calculator.py").read_text()


async def test_failure_then_repair_round(rt):
    p = rt.router.provider("mock")
    p.script = [
        PLAN_TESTS,
        act("first attempt (wrong)", ("write_file", {"path": "app.py", "content": "def greet(name):\n    return 'hello ' + name\n"}), ("tests", {}), done=True, final="done"),
        act("reading failure", ("read_file", {"path": "app.py"})),
        act("correct fix", ("write_file", {"path": "app.py", "content": "def greet(name):\n    return 'hi ' + name\n"}), ("tests", {})),
        act("verified", done=True, final="fixed greet"),
    ]
    rep = await rt.agent.run("make greet say hi")
    assert rep.success and rt.project.reqs.all()[0]["status"] == "PASS"
    roles = [c["role"] for c in p.calls]
    assert roles[0] == "planner" and "implementer" in roles and "debugger" in roles           # repair round switched to the debugger role
    assert any(e.label == "RETRY" for e in rt.bus.buffer)
    # debugger saw the real failure output, not a summary of model confidence
    dbg_first = next(c for c in p.calls if c["role"] == "debugger")["messages"][0]["content"]
    assert "FAILED" in dbg_first or "assert" in dbg_first


async def test_model_claiming_success_does_not_make_requirement_pass(rt):
    rt.router.provider("mock").script = [PLAN_TESTS, act("all good!", done=True, final="Everything works perfectly, 100% confident")] * 3
    rep = await rt.agent.run("make greet say hi")
    assert not rep.success and rt.project.reqs.all()[0]["status"] == "FAIL"
    assert rep.status == "incomplete" and any("FAIL" in l for l in rep.limitations)


async def test_identical_failure_stops_repair_loop(rt):
    same = [act("no-op", done=True, final="nothing")]
    rt.router.provider("mock").script = [PLAN_TESTS] + same * 4
    rep = await rt.agent.run("make greet say hi")
    assert any(e.label == "STUCK" for e in rt.bus.buffer) and any("identical failure" in l for l in rep.limitations)
    assert sum(1 for c in rt.router.provider("mock").calls if c["role"] in ("implementer", "debugger")) <= 3


async def test_repeated_action_triggers_stuck_detection(rt):
    read = act("looking", ("read_file", {"path": "app.py"}))
    rt.router.provider("mock").script = [PLAN_TESTS] + [read] * 12
    rep = await rt.agent.run("make greet say hi")
    stuck = [e for e in rt.bus.buffer if e.label == "STUCK"]
    assert stuck and "repeated" in stuck[0].message
    assert not rep.success


async def test_stop_mid_run_keeps_checkpoint_and_marks_stopped(rt):
    p = rt.router.provider("mock")
    p.script = [PLAN_TESTS, act("slow", ("read_file", {"path": "app.py"}))] * 5
    p.delay = 0.05
    task = asyncio.create_task(rt.agent.run("make greet say hi"))
    await asyncio.sleep(0.25)
    assert rt.agent.state["status"] == "running"
    rt.agent.stop()
    rep = await task
    assert rep.status == "stopped" and rt.project.checkpoints.list() and rt.agent.state["status"] == "idle"
    assert rt.project.last_session()["status"] == "stopped"


async def test_pause_blocks_progress_until_resumed(rt):
    p = rt.router.provider("mock")
    p.script = [PLAN_TESTS, act("x", ("write_file", {"path": "app.py", "content": "def greet(name):\n    return 'hi ' + name\n"}), ("tests", {}), done=True, final="ok")]
    rt.agent.pause()
    task = asyncio.create_task(rt.agent.run("make greet say hi"))
    await asyncio.sleep(0.2)
    assert not task.done() and rt.agent.state["status"] == "paused" and not p.calls
    rt.agent.resume()
    rep = await asyncio.wait_for(task, 20)
    assert rep.success


async def test_plan_mode_modifies_nothing(rt):
    before = (rt.project.root / "app.py").read_text()
    plan = await rt.agent.plan("make greet say hi")
    assert plan.requirements and plan.files_likely
    assert rt.project.reqs.all() == [] and rt.project.tasks.flat() == [] and rt.project.checkpoints.list() == []
    assert (rt.project.root / "app.py").read_text() == before


async def test_safe_mode_headless_cannot_write(rt):
    rt.perms.mode = "safe"
    rt.router.provider("mock").script = [PLAN_TESTS, act("try", ("write_file", {"path": "app.py", "content": "x = 1\n"}), done=True, final="done")] * 3
    rep = await rt.agent.run("make greet say hi")
    assert (rt.project.root / "app.py").read_text().startswith("def greet(name):\n    return 'bye")
    assert not rep.success


async def test_safe_mode_with_approval_writes(rt):
    asked = []
    async def yes(t, r): asked.append(t); return True
    rt.perms.mode, rt.perms.ask = "safe", yes
    rt.router.provider("mock").script = [PLAN_TESTS, act("fix", ("write_file", {"path": "app.py", "content": "def greet(name):\n    return 'hi ' + name\n"}), ("tests", {}), done=True, final="ok")]
    rep = await rt.agent.run("make greet say hi")
    assert rep.success and any("write app.py" in a for a in asked)


async def test_protected_path_is_respected_by_agent(rt):
    rt.perms.protected.append("app.py")
    rt.router.provider("mock").script = [PLAN_TESTS, act("edit", ("write_file", {"path": "app.py", "content": "x=1\n"}), done=True, final="done")] * 3
    await rt.agent.run("make greet say hi")
    assert "bye" in (rt.project.root / "app.py").read_text()


async def test_no_provider_reports_blocker_instead_of_crashing(py_project, make_rt):
    rt = make_rt(py_project, {})
    rep = await rt.agent.run("do something")
    assert rep.status == "blocked" and rep.blockers and "provider" in rep.blockers[0]


async def test_fallback_provider_completes_run_when_primary_down(demo_dir, make_rt):
    rt = make_rt(demo_dir, {"primary": {"tier": 3, "local": False}, "backup": {"tier": 1, "local": False}})
    rt.project.cfg.set("fallback.chain", ["backup"], "project")
    rt.router.mode_override = "max_quality"
    rt.router.provider("primary").fail_with = ProviderError("outage", "503")
    rep = await rt.agent.run("Fix the failing tests in the calculator")
    assert rep.success
    assert any(e.label == "FALLBACK" for e in rt.bus.buffer) and rt.router.provider("backup").calls


async def test_resume_continues_open_requirements_without_replanning(rt):
    p = rt.router.provider("mock")
    p.script = [PLAN_TESTS, act("stop early", done=True, final="nothing")] * 1 + [act("stop early", done=True, final="nothing")] * 3
    await rt.agent.run("make greet say hi")            # fails to fix
    assert rt.project.reqs.all()[0]["status"] == "FAIL"
    n_planner = sum(1 for c in p.calls if c["role"] == "planner")
    p.script = [act("fix", ("write_file", {"path": "app.py", "content": "def greet(name):\n    return 'hi ' + name\n"}), ("tests", {}), done=True, final="ok")]
    rep = await rt.agent.run("")
    assert rep.success and sum(1 for c in p.calls if c["role"] == "planner") == n_planner
    assert len(rt.project.reqs.all()) == 1                                         # no duplicate requirements


async def test_context_compaction_writes_memory(py_project, make_rt):
    rt = make_rt(py_project, {"mock": {"context_window": 4000}})
    (py_project / "big.txt").write_text("\n".join(f"line {i} " + "x" * 60 for i in range(60)))
    reads = [act(f"read {i}", ("read_file", {"path": "big.txt", "start": 1 + i, "end": 40 + i})) for i in range(9)]
    rt.router.provider("mock").script = [PLAN_TESTS] + reads + [act("done", done=True, final="ok")] * 3
    await rt.agent.run("inspect big.txt")
    assert any(e.label == "COMPACT" for e in rt.bus.buffer)
    assert "compacted context" in (rt.project.gdir / "progress.md").read_text()


async def test_malformed_model_output_is_retried_then_abandoned(rt):
    rt.router.provider("mock").script = [PLAN_TESTS, "I will now fix this.", "still not json", "nope"] + ["nope"] * 6
    rep = await rt.agent.run("make greet say hi")
    assert not rep.success and any(e.category == "ERRORS" and "invalid JSON" in e.message for e in rt.bus.buffer)


async def test_visual_qa_not_run_for_non_web_project(demo_rt):
    rep = await demo_rt.agent.run("Fix the failing tests in the calculator")
    assert rep.visual_qa == "not applicable"


async def test_final_review_skipped_for_tiny_changes_but_runs_for_large(rt):
    p = rt.router.provider("mock")
    files = [("write_file", {"path": f"m{i}.py", "content": "x = 1\n" * 30}) for i in range(4)]
    files.append(("write_file", {"path": "app.py", "content": "def greet(name):\n    return 'hi ' + name\n"}))
    p.script = [PLAN_TESTS, act("big", *files, ("tests", {}), done=True, final="ok")]
    rep = await rt.agent.run("make greet say hi")
    assert "final_reviewer" in [c["role"] for c in p.calls] and rep.review


# ---------------- human override via the controller ----------------
async def test_controller_overrides(demo_rt):
    said = []
    demo_rt.bus.subscribe(lambda e: said.append(e.message) if e.label == "SAY" else None)
    ctl = Controller(demo_rt)
    demo_rt.project.cfg.save_provider(__import__("genius_dev.config", fromlist=["x"]).ProviderConfig.from_dict("qwen", {"kind": "mock", "model": "q", "local": True}), "project")
    demo_rt.router.refresh()
    await ctl.handle("use qwen for coding")
    assert demo_rt.router.pins["implementer"] == "qwen" and demo_rt.router.route("implementer").primary == "qwen"
    await ctl.handle("change model to mock")
    assert demo_rt.router.pins["planner"] == "mock"
    (demo_rt.project.root / "tests").mkdir(exist_ok=True)
    await ctl.handle("don't touch the tests")
    assert "tests" in demo_rt.perms.protected and "tests" in demo_rt.project.cfg.get("protect.paths")
    rid = demo_rt.project.reqs.add("something", verify="manual")
    await ctl.handle("skip this requirement")
    assert demo_rt.project.reqs.get(rid)["status"] == "WAIVED"
    demo_rt.project.checkpoints.create("a")
    (demo_rt.project.root / "calculator.py").write_text("junk\n")
    await ctl.handle("restore last checkpoint")
    assert "def add" in (demo_rt.project.root / "calculator.py").read_text()
    await ctl.handle("stop")
    assert demo_rt.agent._stop is True


async def test_controller_runs_goal_and_refuses_concurrent_runs(demo_rt):
    ctl = Controller(demo_rt)
    await ctl.handle("Fix the failing tests in the calculator")
    assert ctl.busy
    await ctl.handle("something else")
    await ctl.task
    assert ctl.last_report.success


# ---------------- crash recovery ----------------
def test_crash_recovery_marks_interrupted_state(demo_rt):
    p = demo_rt.project
    sid = p.start_session("interrupted goal")
    p.store.execute("INSERT INTO tool_calls(ts,session_id,tool,args,status) VALUES(1,?,?,?,'running')", (sid, "terminal", "{}"))
    res = recover_crashed_state(p, demo_rt.bus)
    assert res["sessions"] == 1 and res["tool_calls"] == 1
    assert p.last_session()["status"] == "interrupted" and p.store.one("SELECT status FROM tool_calls")["status"] == "interrupted"
    assert any(e.label == "RECOVER" for e in demo_rt.bus.buffer)


async def test_resume_after_crash_reinspects_and_continues(demo_rt):
    from genius_dev.status import resume_view, resume_text
    p = demo_rt.project
    rid = p.reqs.add("add works", verify="tests")
    p.reqs.set_status(rid, "IN_PROGRESS")
    sid = p.start_session("Fix calculator")
    p.store.execute("INSERT INTO tool_calls(ts,session_id,tool,args,status) VALUES(1,?,?,?,'running')", (sid, "patch_file", "{}"))
    v = resume_view(demo_rt)
    txt = resume_text(v)
    assert "RECOVERY" in txt and "interrupted" in p.last_session()["status"] and "LAST SESSION" in txt and "COMPLETION" in txt
    assert v["open_reqs"]


async def test_stuck_loop_escalates_to_stronger_role(rt):
    read = act("looking", ("read_file", {"path": "app.py"}))
    rt.router.provider("mock").script = [PLAN_TESTS] + [read] * 12
    await rt.agent.run("make greet say hi")
    assert any(e.label == "ESCALATE" for e in rt.bus.buffer)
    assert "debugger" in [c["role"] for c in rt.router.provider("mock").calls]
