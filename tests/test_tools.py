import json

import pytest

from genius_dev.permissions import Permissions
from genius_dev.project import Project
from genius_dev.events import EventBus
from genius_dev.tools import ToolBox


@pytest.fixture
def tb(py_project):
    p = Project.open(py_project)
    return ToolBox(p, Permissions("standard", p.root), EventBus(p.store)), p


async def test_read_write_patch_roundtrip(tb):
    t, p = tb
    r = await t.execute("read_file", {"path": "app.py"})
    assert r.ok and "def greet" in r.output and r.files == ["app.py"]
    assert (await t.execute("write_file", {"path": "new/dir/x.txt", "content": "a\nb"})).ok
    assert (p.root / "new/dir/x.txt").read_text() == "a\nb"
    r = await t.execute("patch_file", {"path": "app.py", "search": "'hi '", "replace": "'hello '"})
    assert r.ok and "hello" in (p.root / "app.py").read_text() and t.changed >= {"app.py", "new/dir/x.txt"}


async def test_patch_requires_unique_exact_match(tb):
    t, p = tb
    (p.root / "d.txt").write_text("x\nx\n")
    assert not (await t.execute("patch_file", {"path": "d.txt", "search": "x", "replace": "y"})).ok
    assert not (await t.execute("patch_file", {"path": "d.txt", "search": "zzz", "replace": "y"})).ok
    assert (await t.execute("patch_file", {"path": "d.txt", "search": "x", "replace": "y", "replace_all": True})).ok
    assert (p.root / "d.txt").read_text() == "y\ny\n"


async def test_paths_outside_project_are_refused(tb):
    t, p = tb
    r = await t.execute("read_file", {"path": "../../etc/hosts"})
    assert not r.ok and r.denied
    r = await t.execute("write_file", {"path": "../escape.txt", "content": "x"})
    assert not r.ok and r.denied and not (p.root.parent / "escape.txt").exists()


async def test_secret_files_are_never_read_to_the_model(tb):
    t, p = tb
    (p.root / ".env").write_text("API_KEY=supersecretvalue123456\n")
    r = await t.execute("read_file", {"path": ".env"})
    assert not r.ok and "withheld" in r.summary and "supersecret" not in r.output


async def test_output_secrets_are_redacted(tb):
    t, p = tb
    (p.root / "cfg.py").write_text("token = 'ghp_" + "a" * 36 + "'\n")
    r = await t.execute("read_file", {"path": "cfg.py"})
    assert "ghp_aaaa" not in r.output


async def test_protected_paths_denied_headless_and_allowed_when_approved(tmp_path, py_project):
    p = Project.open(py_project)
    (p.root / "auth").mkdir()
    (p.root / "auth" / "login.py").write_text("x = 1\n")
    perms = Permissions("autonomous", p.root, protected=["auth"])
    t = ToolBox(p, perms, EventBus())
    r = await t.execute("write_file", {"path": "auth/login.py", "content": "hacked"})
    assert r.denied and (p.root / "auth/login.py").read_text() == "x = 1\n"
    async def yes(title, reason): return True
    perms.ask = yes
    assert (await t.execute("write_file", {"path": "auth/login.py", "content": "ok"})).ok


async def test_terminal_gates_by_risk(tb):
    t, p = tb
    assert (await t.execute("terminal", {"command": "echo hello"})).output.strip() == "hello"
    r = await t.execute("terminal", {"command": "rm app.py"})
    assert r.denied and (p.root / "app.py").exists()                 # standard mode, headless: asked -> denied
    r = await t.execute("terminal", {"command": "sudo rm -rf /"})
    assert r.denied
    r = await t.execute("terminal", {"command": "python3 -c 'raise SystemExit(3)'"})
    assert not r.ok and r.data["code"] == 3


async def test_delete_always_confirms_except_autonomous(tb):
    t, p = tb
    (p.root / "junk.txt").write_text("x")
    assert (await t.execute("delete_file", {"path": "junk.txt"})).denied and (p.root / "junk.txt").exists()
    t.perms.mode = "autonomous"
    assert (await t.execute("delete_file", {"path": "junk.txt"})).ok and not (p.root / "junk.txt").exists()
    assert (await t.execute("delete_file", {"path": "."})).denied


async def test_tool_calls_are_logged_with_duration_and_files(tb):
    t, p = tb
    await t.execute("write_file", {"path": "a.txt", "content": "1"})
    await t.execute("read_file", {"path": "missing.txt"})
    rows = p.store.query("SELECT * FROM tool_calls ORDER BY id")
    assert [r["tool"] for r in rows] == ["write_file", "read_file"]
    assert rows[0]["ok"] == 1 and rows[1]["ok"] == 0 and rows[0]["duration"] >= 0 and json.loads(rows[0]["files"]) == ["a.txt"]
    assert all(r["status"] == "done" for r in rows)


async def test_bad_arguments_and_unknown_tool_are_structured_errors(tb):
    t, _ = tb
    assert not (await t.execute("read_file", {})).ok
    assert not (await t.execute("read_file", {"path": "a", "bogus": 1})).ok
    assert "unknown tool" in (await t.execute("nope", {})).summary


async def test_tests_tool_runs_and_parses(tb):
    t, p = tb
    r = await t.execute("tests", {})
    assert r.ok and r.data["passed"] == 1 and r.data["total"] == 1
    (p.root / "tests" / "test_app.py").write_text("def test_bad():\n    assert 1 == 2\n")
    r = await t.execute("tests", {})
    assert not r.ok and r.data["failed"] == 1 and r.failure_class == "test_failure"
    assert p.last_run("tests")["failed"] == 1


async def test_search_and_index_tools(tb):
    t, _ = tb
    r = await t.execute("search_files", {"pattern": "greet"})
    assert "app.py" in r.output
    r = await t.execute("search_symbols", {"query": "Thing"})
    assert "Thing" in r.output


async def test_git_tool_blocks_publishing_and_destructive_verbs(tb):
    t, _ = tb
    for v in ("push origin main", "reset --hard", "clean -fd", "checkout ."):
        assert (await t.execute("git", {"args": v})).denied
    assert (await t.execute("git", {"args": "status --short"})).ok


async def test_http_external_needs_approval_localhost_does_not(tb):
    t, _ = tb
    r = await t.execute("http", {"url": "https://example.com/x"})
    assert r.denied
    r = await t.execute("http", {"url": "http://127.0.0.1:9/x"})
    assert not r.denied and not r.ok           # connection refused, but not blocked by permissions


async def test_database_tool_read_only_by_default(tb):
    import sqlite3
    t, p = tb
    con = sqlite3.connect(p.root / "a.db"); con.execute("create table t(x)"); con.execute("insert into t values (1)"); con.commit(); con.close()
    r = await t.execute("database", {"path": "a.db", "sql": "select * from t"})
    assert r.ok and "1" in r.output
    assert (await t.execute("database", {"path": "a.db", "sql": "drop table t"})).denied


async def test_env_inspect_never_lists_secret_values(tb, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "supersecret")
    t, _ = tb
    r = await t.execute("env_inspect", {})
    assert "supersecret" not in r.output and "MY_API_KEY" not in r.output


async def test_approval_prompt_shows_the_actual_change(tb):
    t, p = tb
    t.perms.mode = "safe"
    seen = []
    async def ask(title, reason):
        seen.append((title, reason)); return False
    t.perms.ask = ask
    r = await t.execute("patch_file", {"path": "app.py", "search": "'hi '", "replace": "'yo '"})
    assert r.denied and "app.py" in seen[0][0] and "-    return 'hi ' + name" in seen[0][1] and "+    return 'yo ' + name" in seen[0][1]
    assert "hi " in (p.root / "app.py").read_text()
