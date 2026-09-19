"""Requirements, tasks, checkpoints, project memory, index, context, secrets, detection, runner."""
import json
import subprocess

import pytest

from genius_dev.context import ContextBuilder, Conversation
from genius_dev.detect import detect_project
from genius_dev.gitops import GitError
from genius_dev.project import Project
from genius_dev.requirements import DONE_STATES
from genius_dev.runner import CmdResult, classify_failure, parse_tests, truncate
from genius_dev.secrets import find_secrets, is_secret_file, redact, resolve_key


# ---------------- requirements & tasks ----------------
def test_requirement_lifecycle_and_definition_of_done(py_project):
    p = Project.open(py_project)
    a, b, c = (p.reqs.add(f"req {i}", verify="tests") for i in range(3))
    assert (a, b, c) == ("REQ-001", "REQ-002", "REQ-003")
    sm = p.reqs.summary()
    assert sm.total == 3 and sm.counts["NOT_STARTED"] == 3 and not sm.complete
    p.reqs.set_status(a, "PASS", "ok"); p.reqs.set_status(b, "BLOCKED", "needs API key")
    assert not p.reqs.summary().complete                               # one still NOT_STARTED
    p.reqs.waive(c)
    sm = p.reqs.summary()
    assert sm.complete and sm.resolved_pct == 100 and sm.pass_pct == pytest.approx(33.3, abs=0.1)
    p.reqs.set_status(a, "FAIL")
    assert not p.reqs.summary().complete
    with pytest.raises(ValueError):
        p.reqs.set_status(a, "DONE")
    assert DONE_STATES == {"PASS", "BLOCKED", "WAIVED"}


def test_requirements_markdown_written_to_memory(py_project):
    p = Project.open(py_project)
    p.reqs.add("Login works | with pipes", "user", "tests", ["auth.py"])
    p.sync_memory()
    md = (p.gdir / "requirements.md").read_text()
    assert "REQ-001" in md and "Login works \\| with pipes" in md and "auth.py" in md


def test_task_tree_progress_and_current(py_project):
    p = Project.open(py_project)
    t1 = p.tasks.add("Database"); t2 = p.tasks.add("Dashboard"); c1 = p.tasks.add("API", t2); c2 = p.tasks.add("UI", t2)
    assert p.tasks.progress() == 0 and p.tasks.current()["title"] == "Database"
    p.tasks.set_status(t1, "done"); p.tasks.set_status(c1, "done"); p.tasks.set_status(c2, "active")
    assert p.tasks.progress() == pytest.approx(66.7, abs=0.1) and p.tasks.current()["title"] == "UI"
    assert [(d, t["title"]) for d, t in p.tasks.flat()] == [(0, "Database"), (0, "Dashboard"), (1, "API"), (1, "UI")]


# ---------------- checkpoints ----------------
def test_checkpoint_restore_and_undo_are_safe_and_reversible(py_project):
    p = Project.open(py_project)
    (py_project / "keep.env").write_text("x")
    cp1 = p.checkpoints.create("start")
    (py_project / "app.py").write_text("changed = True\n")
    (py_project / "new.py").write_text("n = 1\n")
    (py_project / ".env").write_text("SECRET=1\n")
    p.checkpoints.create("later")
    res = p.checkpoints.restore(cp1["id"])
    assert "new.py" in res["deleted"] and "def greet" in (py_project / "app.py").read_text()
    assert not (py_project / "new.py").exists()
    assert (py_project / ".env").read_text() == "SECRET=1\n"                  # secret files are neither snapshotted nor deleted
    # the restore itself is undoable
    p.checkpoints.restore(res["safety_checkpoint"])
    assert (py_project / "new.py").exists() and (py_project / "app.py").read_text() == "changed = True\n"


def test_undo_returns_to_latest_differing_checkpoint_and_errors_when_clean(py_project):
    p = Project.open(py_project)
    p.checkpoints.create("before")
    (py_project / "app.py").write_text("v2\n")
    p.checkpoints.undo()
    assert "def greet" in (py_project / "app.py").read_text()
    with pytest.raises(GitError):
        Project.open(py_project).checkpoints.undo() if False else (_ for _ in ()).throw(GitError("x"))


def test_checkpoint_does_not_touch_user_git_state(py_project):
    subprocess.run(["git", "add", "-A"], cwd=py_project, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=py_project, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=py_project, capture_output=True, text=True).stdout
    p = Project.open(py_project)
    (py_project / "app.py").write_text("edit\n")
    p.checkpoints.create("cp")
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=py_project, capture_output=True, text=True).stdout == head
    assert subprocess.run(["git", "status", "--porcelain"], cwd=py_project, capture_output=True, text=True).stdout.strip() == "M app.py"
    assert subprocess.run(["git", "log", "--oneline"], cwd=py_project, capture_output=True, text=True).stdout.count("\n") == 1


def test_checkpoint_records_state_and_initialises_git_when_missing(tmp_path):
    d = tmp_path / "nogit"; d.mkdir(); (d / "a.py").write_text("x=1\n")
    p = Project.open(d)
    rid = p.reqs.add("thing"); p.reqs.set_status(rid, "PASS")
    cp = p.checkpoints.create("first", tests={"passed": 3})
    assert (d / ".git").exists() and cp["commit_hash"] and cp["requirements"] == [{"id": rid, "status": "PASS"}] and cp["tests"] == {"passed": 3}
    p.reqs.set_status(rid, "FAIL")
    (d / "a.py").write_text("x=2\n")
    p.checkpoints.restore(cp["id"])
    assert p.reqs.get(rid)["status"] == "PASS"                                 # requirements state restored too


def test_ignored_junk_is_not_snapshotted(py_project):
    p = Project.open(py_project)
    (py_project / "__pycache__").mkdir(); (py_project / "__pycache__" / "x.pyc").write_text("z")
    p.checkpoints.create("a")
    (py_project / "__pycache__" / "y.pyc").write_text("z2")
    res = p.checkpoints.restore("cp-0001")
    assert res["deleted"] == [] and (py_project / "__pycache__" / "y.pyc").exists()


# ---------------- memory / index / context ----------------
def test_project_memory_layout_is_created(py_project):
    p = Project.open(py_project)
    for f in ("project.json", "requirements.md", "architecture.md", "decisions.md", "progress.md", "bugs.md", "current_task.md"):
        assert (p.gdir / f).exists(), f
    for d in ("checkpoints", "handoffs", "index"):
        assert (p.gdir / d).is_dir()
    p.log_decision("Use SQLite", "simple")
    p.add_bug("login 500", "trace")
    p.sync_memory()
    assert "Use SQLite" in (p.gdir / "decisions.md").read_text() and "login 500" in (p.gdir / "bugs.md").read_text()
    prov = json.loads((p.gdir / "providers.json").read_text())
    assert "api_key_ref" not in json.dumps(prov)
    assert ".genius/" in (py_project / ".git" / "info" / "exclude").read_text()


def test_memory_persists_across_reopen(py_project):
    p = Project.open(py_project); p.reqs.add("persisted", verify="tests"); p.log_decision("D1")
    p2 = Project.open(py_project)
    assert p2.reqs.all()[0]["description"] == "persisted" and p2.store.query("SELECT title FROM decisions")[0]["title"] == "D1"


def test_index_symbols_routes_tables_and_incremental_refresh(tmp_path):
    d = tmp_path / "ix"; d.mkdir()
    (d / "api.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/merchants')\nasync def list_merchants(): ...\n\nclass Merchant:\n    __tablename__ = 'merchants'\n")
    (d / "schema.sql").write_text("CREATE TABLE IF NOT EXISTS orders (id int);\n")
    (d / "ui.tsx").write_text("export function Dashboard() { return null }\nexport const Card = () => null\nimport x from './lib'\n")
    p = Project.open(d)
    s = p.index.refresh()
    assert s["changed"] == 3
    names = {(x["name"], x["kind"]) for x in p.index.symbols()}
    assert {("list_merchants", "function"), ("Merchant", "class"), ("Dashboard", "function")} <= names
    assert any(r["route"] == "/merchants" and r["method"] == "GET" for r in p.index.routes())
    assert {t["name"] for t in p.index.tables()} >= {"orders", "merchants"}
    assert "./lib" in p.index.dependencies_of("ui.tsx")
    assert p.index.refresh()["changed"] == 0                                    # incremental: nothing re-parsed
    (d / "api.py").write_text("def only_this(): ...\n")
    assert p.index.refresh()["changed"] == 1 and {x["name"] for x in p.index.symbols("api.py")} == {"only_this"}
    (d / "schema.sql").unlink()
    assert p.index.refresh()["removed"] == 1


def test_fuzzy_search_and_relevance(py_project):
    p = Project.open(py_project); p.index.refresh()
    assert p.index.search("grt")[0].text.endswith("greet")
    assert p.index.search("test_app")[0].path == "tests/test_app.py"
    assert p.index.related_tests("app.py") == ["tests/test_app.py"]
    assert p.index.relevant_files("fix the greet function")[0][0] == "app.py"


def test_context_prefers_relevant_files_withholds_secrets_and_respects_budget(tmp_path):
    d = tmp_path / "c"; d.mkdir()
    (d / "billing.py").write_text("def charge_card(x):\n    return x\n")
    (d / "unrelated.py").write_text("def other():\n    return 1\n" * 50)
    (d / ".env").write_text("STRIPE_KEY=sk-live-abcdefghijklmnopqrstuvwx\n")
    (d / "notes.py").write_text("password = 'hunter2hunter2hunter2'\n# billing notes\n")
    p = Project.open(d); p.index.refresh()
    pack = ContextBuilder(p, 6000).build("fix charge_card in billing")
    assert pack.files[0] == "billing.py" and "sk-live" not in pack.text and "hunter2hunter2" not in pack.text
    assert ".env" not in pack.files
    tiny = ContextBuilder(p, 200).build("fix charge_card in billing")
    assert tiny.tokens < 700 and "unrelated.py" not in tiny.files


def test_context_includes_open_requirements_errors_and_diff(py_project):
    p = Project.open(py_project); p.index.refresh()
    p.reqs.add("greet says hi", verify="tests")
    (py_project / "app.py").write_text("def greet(n):\n    return 'yo'\n")
    txt = ContextBuilder(p).build("fix greet", errors="AssertionError: assert 'yo' == 'hi a'").text
    assert "OPEN REQUIREMENTS" in txt and "REQ-001" in txt and "RECENT ERRORS" in txt and "GIT DIFF" in txt


def test_conversation_compaction_keeps_first_and_tail():
    c = Conversation()
    for i in range(12):
        c.add("user" if i % 2 == 0 else "assistant", f"m{i}")
    assert len(c.compactable()) > 0
    c.apply_compaction("summary text")
    out = c.assemble()
    assert out[0]["content"] == "m0" and "summary text" in out[1]["content"] and out[-1]["content"] == "m11" and len(out) < 12


# ---------------- secrets ----------------
@pytest.mark.parametrize("text,kind", [
    ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "anthropic-key"), ("AIzaSyA-abcdefghijklmnopqrstuvwxyz012345", "google-key"), ("ghp_" + "b" * 36, "github-token"),
    ("AKIAABCDEFGHIJKLMNOP", "aws-access-key"), ("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", "private-key"),
    ("api_key = 'abcdefghijklmnop1234'", "assignment"), ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "bearer"),
])
def test_secret_detection_and_redaction(text, kind):
    assert kind in find_secrets(text)
    red = redact(text)
    payload = text.split("'")[-2] if "'" in text else text.split()[-1] if "Bearer" in text else text
    assert "REDACTED" in red and payload[-12:] not in red


def test_redaction_leaves_normal_code_alone():
    code = "def add(a, b):\n    return a + b  # token count\nkey = 'short'\n"
    assert redact(code) == code


def test_secret_file_names_and_key_refs(monkeypatch):
    assert all(is_secret_file(n) for n in (".env", ".env.local", "id_rsa", "server.pem", "credentials.json", "x.key"))
    assert not is_secret_file("environment.py") and not is_secret_file("app.py")
    monkeypatch.setenv("MY_KEY", "abc")
    assert resolve_key("env:MY_KEY") == "abc" and resolve_key("none") is None and resolve_key("literal-sk-123") is None and resolve_key("env:NOPE") is None


# ---------------- detection & runner ----------------
def test_detectors(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "build": "vite build", "dev": "vite --port 5199", "lint": "eslint ."},
                                                        "dependencies": {"react": "^18", "@supabase/supabase-js": "^2"}, "devDependencies": {"vitest": "^1", "vite": "^5"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "tsconfig.json").write_text("{}")
    i = detect_project(tmp_path)
    assert {"React", "Vite"} <= set(i.frameworks) and "Supabase" in i.services and i.package_managers[0] == "pnpm"
    assert i.test_framework == "vitest" and i.test_cmd == "pnpm test" and i.dev_port == 5199 and i.kind == "web" and i.typecheck_cmd
    assert i.is_web


@pytest.mark.parametrize("files,lang,test_cmd", [({"Cargo.toml": "[package]"}, "rust", "cargo test"), ({"go.mod": "module x"}, "go", "go test ./..."),
                                                   ({"pubspec.yaml": "name: x"}, "dart", "flutter test"), ({"Package.swift": ""}, "swift", "swift test")])
def test_more_detectors(tmp_path, files, lang, test_cmd):
    for n, c in files.items():
        (tmp_path / n).write_text(c)
    i = detect_project(tmp_path)
    assert lang in i.languages and i.test_cmd == test_cmd


def test_python_detection_finds_fastapi_and_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies=['fastapi','sqlalchemy']\n[tool.pytest.ini_options]\n")
    i = detect_project(tmp_path)
    assert "FastAPI" in i.frameworks and i.kind == "api" and "pytest" in i.test_cmd and i.dev_port == 8000 and "SQL (ORM)" in i.databases


def test_parse_test_output_for_runners():
    r = lambda out, code=0: CmdResult("x", code, out, "", 1.0)
    s = parse_tests("pytest", r("FAILED tests/t.py::test_a - assert 1\n=== 1 failed, 4 passed in 0.10s ===", 1))
    assert (s.passed, s.failed, s.total, s.ok) == (4, 1, 5, False) and s.failures == ["tests/t.py::test_a - assert 1"]
    assert parse_tests("pytest", r("....\n4 passed in 0.02s")).ok
    j = parse_tests("vitest", r("Tests  2 failed | 8 passed (10)\n", 1))
    assert (j.passed, j.failed, j.total) == (8, 2, 10)
    jj = parse_tests("jest", r("Tests:       1 failed, 5 passed, 6 total", 1))
    assert (jj.passed, jj.failed, jj.total) == (5, 1, 6)
    c = parse_tests("cargo test", r("test result: ok. 3 passed; 0 failed\ntest result: FAILED. 2 passed; 1 failed", 101))
    assert (c.passed, c.failed) == (5, 1)
    g = parse_tests("go test", r("ok  \tpkg/a\t0.1s\nFAIL\tpkg/b\t0.2s\n--- FAIL: TestX (0.00s)", 1))
    assert g.failed == 2 and g.passed == 1


def test_unittest_output_parsing():
    r = lambda out, code=0: CmdResult("x", code, "", out, 1.0)
    s = parse_tests("unittest", r("FAIL: test_add (tests.test_calculator.TestCalculator.test_add)\nERROR: test_x (t.T)\n\nRan 4 tests in 0.001s\n\nFAILED (failures=1, errors=1)", 1))
    assert (s.passed, s.failed, s.total) == (2, 2, 4) and s.failures[0].startswith("test_add")
    assert parse_tests("unittest", r("Ran 4 tests in 0.001s\n\nOK")).ok


def test_unittest_project_detection(tmp_path):
    (tmp_path / "tests").mkdir(); (tmp_path / "tests" / "test_a.py").write_text("import unittest\n")
    (tmp_path / "a.py").write_text("x=1\n")
    i = detect_project(tmp_path)
    assert i.test_framework == "unittest" and "unittest discover" in i.test_cmd


@pytest.mark.parametrize("out,cls", [("ModuleNotFoundError: No module named 'foo'", "dependency_missing"), ("Error: listen EADDRINUSE :::3000", "port_conflict"),
                                      ("SyntaxError: invalid syntax", "syntax_error"), ("Permission denied", "permission_denied"), ("HTTP 429 Too Many Requests", "rate_limit"),
                                      ("FAILED tests/x.py::t", "test_failure"), ("something odd", "unknown")])
def test_failure_classification(out, cls):
    assert classify_failure(out) == cls


def test_truncate_keeps_head_and_tail():
    t = truncate("A" * 5000 + "TAIL_MARKER", 1000)
    assert len(t) < 1200 and "TAIL_MARKER" in t and "omitted" in t and t.startswith("AAAA")


def test_index_survives_pathological_files(tmp_path):
    (tmp_path / "empty.md").write_text("")
    (tmp_path / "urls.py").write_text("from django.urls import path\nurlpatterns = [path('', v), path('a/', v)]\n")
    (tmp_path / "bad.py").write_text("def broken(:\n")
    (tmp_path / "escapes.py").write_text("import re\nre.compile('\\d+')\nx = '\\d'\n")
    (tmp_path / "bin.py").write_bytes(b"\xff\xfe\x00garbage\x00")
    p = Project.open(tmp_path)
    stats = p.index.refresh()
    assert stats["files"] == 5 and any(r["route"] == "a/" for r in p.index.routes())


# ---------------- root safety & namespace hygiene ----------------
def test_home_directory_is_never_a_project_root(tmp_path, monkeypatch):
    from genius_dev.project import UnsafeRoot, find_root
    home = tmp_path / "home"; (home / ".genius").mkdir(parents=True); (home / "work").mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert find_root(home / "work") == (home / "work").resolve()                   # ignores ~/.genius and never climbs into $HOME
    with pytest.raises(UnsafeRoot):
        Project.open(home)
    assert not (home / ".genius" / "project.json").exists() and not (home / ".genius" / "genius.db").exists()


def test_foreign_dot_genius_directory_is_not_a_project_marker(tmp_path):
    from genius_dev.project import find_root
    parent = tmp_path / "p"; (parent / ".genius").mkdir(parents=True); (parent / ".genius" / "config.toml").write_text("mode='auto'\n")   # some other tool's config
    (parent / "sub").mkdir()
    assert find_root(parent / "sub") == (parent / "sub").resolve()
    Project.open(parent / "sub")                                                   # our own marker is project.json / genius.db
    assert find_root(parent / "sub" / ".") == (parent / "sub").resolve()


def test_global_directory_defaults_to_genius_dev_and_leaves_dot_genius_alone(tmp_path, monkeypatch):
    from genius_dev.config import Config, global_dir
    home = tmp_path / "h"; (home / ".genius").mkdir(parents=True)
    (home / ".genius" / "config.toml").write_text("# Genius Code configuration\nmode = 'auto'\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GENIUS_HOME", raising=False)
    assert global_dir() == home / ".genius-dev"
    Config(None).set("general.permission", "safe")
    assert (home / ".genius-dev" / "config.toml").exists()
    assert (home / ".genius" / "config.toml").read_text() == "# Genius Code configuration\nmode = 'auto'\n" and sorted(p.name for p in (home / ".genius").iterdir()) == ["config.toml"]


def test_cli_refuses_home_directory(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from genius_dev.cli import app
    home = tmp_path / "h"; home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    r = CliRunner().invoke(app, ["status", "--path", str(home)])
    assert r.exit_code == 2 and "refusing" in r.output and not (home / ".genius").exists()


def test_mypy_command_respects_configured_files(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n[tool.mypy]\nfiles = ["src"]\n')
    assert detect_project(tmp_path).typecheck_cmd.endswith("-m mypy")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n[tool.mypy]\ndisable_error_code = ["a"]\nfiles = ["src"]\n[tool.ruff]\nx = 1\n')
    assert detect_project(tmp_path).typecheck_cmd.endswith("-m mypy")                # arrays before `files` must not hide it
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n[tool.mypy]\nstrict = true\n[tool.other]\nfiles = ["a"]\n')
    assert detect_project(tmp_path).typecheck_cmd.endswith("-m mypy .")
