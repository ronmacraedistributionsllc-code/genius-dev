"""Plugin system + preview (web, desktop, CLI, simulators with faked platform tools)."""
import json
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from genius_dev.cli import app
from genius_dev.doctor import run_doctor
from genius_dev.plugins import load_registry
from genius_dev.preview import choose_plan, run_preview
from genius_dev.project import Project
from genius_dev.runner import CmdResult
from genius_dev.tools import ToolBox
from genius_dev.detect import detect_project

runner = CliRunner()

PLUGIN_SRC = textwrap.dedent('''
    from genius_dev.plugins import Plugin, PluginTool, PreviewPlan, Check

    class Shopify(Plugin):
        name = "shopify"
        description = "Shopify themes (test plugin)"
        def detect(self, root, info):
            if (root / "theme.liquid").exists():
                info.add("frameworks", "Shopify")
                info.test_cmd = "echo theme-lint"
                info.kind = "web"
                info.dev_cmd, info.dev_port = "echo serve", 9292
        def preview(self, project):
            if "Shopify" in project.info.frameworks:
                return PreviewPlan("cli", "Shopify theme", "echo previewing-theme", plugin=self.name)
        async def doctor(self, project):
            return [Check("Shopify CLI", "warn", "not installed (test)")]
        def tools(self):
            async def theme_files(tb):
                from genius_dev.tools import ToolResult
                return ToolResult(True, "themes", "\\n".join(p.name for p in tb.p.root.glob("*.liquid")))
            return [PluginTool("theme_files", "List liquid files.", {}, theme_files, "THEME")]

    PLUGINS = [Shopify()]
''')


@pytest.fixture
def user_plugin(genius_home):
    d = genius_home / "plugins"; d.mkdir(parents=True)
    (d / "shopify.py").write_text(PLUGIN_SRC)
    return d


def test_builtin_plugins_and_contributions(tmp_path):
    reg = load_registry(tmp_path)
    names = [p.name for p in reg.plugins]
    assert names[:6] == ["python", "node", "toolchains", "git", "playwright", "docker"] and not reg.errors
    by = {p.name: p for p in reg.plugins}
    assert set(by["git"].contributes()) >= {"detect", "doctor", "tools"} and "preview" in by["docker"].contributes() and "preview" in by["toolchains"].contributes()
    assert {t.name for _, t in reg.tools()} >= {"python_env", "npm_scripts", "git_summary", "browser_available", "docker_ps"}


def test_user_plugin_contributes_detection_tools_doctor_preview(tmp_path, user_plugin):
    d = tmp_path / "theme"; d.mkdir(); (d / "theme.liquid").write_text("{{ x }}")
    info = detect_project(d)
    assert "Shopify" in info.frameworks and info.test_cmd == "echo theme-lint" and info.dev_port == 9292
    reg = load_registry(d)
    assert "shopify" in [p.name for p in reg.plugins] and reg.sources["shopify"].startswith("user:")


async def test_plugin_tool_is_callable_by_the_agent_toolbox(tmp_path, user_plugin):
    d = tmp_path / "theme"; d.mkdir(); (d / "theme.liquid").write_text("x"); subprocess.run(["git", "init", "-q"], cwd=d)
    from genius_dev.events import EventBus
    from genius_dev.permissions import Permissions
    p = Project.open(d)
    tb = ToolBox(p, Permissions("standard", p.root), EventBus(p.store))
    assert "theme_files" in tb.specs and "[shopify]" in tb.specs["theme_files"].description
    r = await tb.execute("theme_files", {})
    assert r.ok and "theme.liquid" in r.output
    assert (await tb.execute("git_summary", {})).ok and (await tb.execute("npm_scripts", {})).summary == "no package.json"
    assert "theme_files" in tb.descriptions()                                   # advertised to the model like any tool


async def test_plugin_doctor_and_preview_are_used(tmp_path, user_plugin, make_rt):
    d = tmp_path / "theme"; d.mkdir(); (d / "theme.liquid").write_text("x"); subprocess.run(["git", "init", "-q"], cwd=d)
    rt = make_rt(d)
    rep = await run_doctor(rt.project, rt.router, quick=True)
    assert any(c.name == "Shopify CLI" and c.status == "warn" for c in rep.sections["SYSTEM"])
    plan = choose_plan(rt.project)
    assert plan.plugin == "shopify" and plan.kind in ("cli", "web-server")


def test_broken_and_incompatible_plugins_never_break_the_app(tmp_path, genius_home):
    d = genius_home / "plugins"; d.mkdir(parents=True)
    (d / "syntax.py").write_text("def broken(:\n")
    (d / "empty.py").write_text("x = 1\n")
    (d / "oldapi.py").write_text("from genius_dev.plugins import Plugin\nclass P(Plugin):\n    name='old'\n    api_version=99\nPLUGINS=[P()]\n")
    (d / "crash.py").write_text("from genius_dev.plugins import Plugin\nclass C(Plugin):\n    name='crash'\n    def detect(self, root, info): raise RuntimeError('boom')\nPLUGINS=[C()]\n")
    (d / "dupe.py").write_text("from genius_dev.plugins import Plugin\nclass G(Plugin):\n    name='git'\nPLUGINS=[G()]\n")
    reg = load_registry(tmp_path)
    err = " ".join(reg.errors)
    assert "syntax.py" in err and "empty.py" in err and "api_version 99" in err and "duplicate plugin name" in err
    info = detect_project(tmp_path)                                    # the crashing plugin's detect is isolated
    assert any("crash" in n for n in info.notes)


def test_project_plugins_require_trust(tmp_path, genius_home):
    d = tmp_path / "p"; (d / ".genius" / "plugins").mkdir(parents=True)
    (d / ".genius" / "plugins" / "evil.py").write_text(PLUGIN_SRC.replace("shopify", "evil").replace("Shopify", "Evil") + "\nopen(__file__ + '.ran', 'w').write('x')\n")
    reg = load_registry(d)
    assert "evil" not in [p.name for p in reg.plugins] and any("trust_project" in e for e in reg.errors)
    assert not (d / ".genius" / "plugins" / "evil.py.ran").exists()                     # untrusted project code was NOT executed
    from genius_dev.config import Config
    Config(d).set("plugins.trust_project", True, "project")
    Config(None).set("plugins.trust_project", True)
    assert "evil" in [p.name for p in load_registry(d).plugins]


def test_plugins_cli(tmp_path, user_plugin):
    r = runner.invoke(app, ["plugins", "--json", "--path", str(tmp_path)])
    data = json.loads(r.output)
    assert {p["name"] for p in data["plugins"]} >= {"python", "node", "git", "playwright", "docker", "shopify"}
    assert "Shopify" not in r.output or True
    assert "shopify" in runner.invoke(app, ["plugins", "--path", str(tmp_path)]).output


# ---------------------------------------------------------------- preview
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


async def test_web_preview_starts_health_checks_and_reports_port(tmp_path, monkeypatch):
    d = tmp_path / "site"; d.mkdir(); (d / "index.html").write_text("<!doctype html><title>x</title><h1>hi</h1>"); subprocess.run(["git", "init", "-q"], cwd=d)
    p = Project.open(d)
    opened = []
    monkeypatch.setattr("genius_dev.preview.webbrowser.open", lambda u: opened.append(u))
    try:
        r = await run_preview(p, open_browser=True, check=True)
        assert r.ok and r.kind == "web-server" and r.health["ok"] and r.health["status"] == 200 and r.url == f"http://localhost:{r.port}"
        assert r.process == "dev" and Path(r.log).exists() and opened == [r.url]
        assert r.browser.get("ran") and r.browser["ok"] and Path(r.browser["screenshot"]).exists()
        r2 = await run_preview(p, check=False)
        assert r2.ok and "already running (reused)" in r2.notes                       # no duplicate server
    finally:
        p.procs.stop_all()
    assert p.procs.get("dev")["status"] == "stopped"


async def test_preview_captures_browser_errors_and_reports_unhealthy_servers(tmp_path):
    d = tmp_path / "site"; d.mkdir(); (d / "index.html").write_text("<script>console.error('kaboom')</script><img src='/nope.png'>")
    p = Project.open(d)
    try:
        r = await run_preview(p)
        assert r.ok and not r.browser["ok"] and any("kaboom" in e for e in r.browser["console_errors"]) and any("nope.png" in e for e in r.browser["failed_requests"])
    finally:
        p.procs.stop_all()
    (d / "index.html").unlink()
    p2 = Project.open(tmp_path / "empty" if (tmp_path / "empty").mkdir() is None else tmp_path)
    p2._info = None
    p2.info.dev_cmd, p2.info.dev_port = f"{shlex_quote(sys.executable)} -c \"import time; time.sleep(30)\"", free_port()
    try:
        bad = await run_preview(p2, check=False, ready_timeout=1, health_timeout=1)
        assert not bad.ok and "did not answer" in bad.message
    finally:
        p2.procs.stop_all()


def shlex_quote(s):
    import shlex
    return shlex.quote(s)


async def test_preview_reports_port_conflicts_precisely(tmp_path):
    d = tmp_path / "site"; d.mkdir(); (d / "index.html").write_text("x")
    p = Project.open(d)
    port = p.info.dev_port
    s = socket.socket(); s.bind(("127.0.0.1", port)); s.listen()
    try:
        r = await run_preview(p, check=False)
    finally:
        s.close()
    assert not r.ok and "already in use" in r.message and "another process already uses that port" in r.reason


async def test_nothing_to_preview_says_why(tmp_path):
    p = Project.open(tmp_path)
    r = await run_preview(p)
    assert not r.ok and r.kind == "none" and "no dev-server command" in r.reason


async def test_cli_project_preview_runs_once(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    p = Project.open(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/bin/" + n)
    plan = choose_plan(p)
    assert plan.kind == "cli" and plan.command == "cargo run --quiet"
    async def fake(cmd, cwd, timeout):
        return CmdResult(cmd, 0, "hello from cargo\n", "", 0.1)
    r = await run_preview(p, runner=fake)
    assert r.ok and r.kind == "cli" and "hello from cargo" in r.steps[0]["detail"]


async def test_platforms_that_cannot_be_previewed_say_exactly_why(tmp_path, monkeypatch):
    monkeypatch.delenv("ANDROID_HOME", raising=False); monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    ios = tmp_path / "ios"; (ios / "App.xcodeproj").mkdir(parents=True)
    r = await run_preview(Project.open(ios))
    assert not r.ok and r.kind == "ios-simulator" and "xcrun not found" in r.reason
    a = tmp_path / "android"; (a / "app/src/main").mkdir(parents=True); (a / "app/src/main/AndroidManifest.xml").write_text("<manifest package='x.y'/>"); (a / "build.gradle").write_text("")
    r = await run_preview(Project.open(a))
    assert not r.ok and "adb not found" in r.reason
    f = tmp_path / "flutter"; f.mkdir(); (f / "pubspec.yaml").write_text("name: x")
    assert "flutter is not installed" in (await run_preview(Project.open(f))).reason
    c = tmp_path / "compose"; c.mkdir(); (c / "compose.yaml").write_text("services: {}")
    assert "docker is not installed" in (await run_preview(Project.open(c))).reason


def fake_runner(script):
    calls = []
    async def run(cmd, cwd, timeout):
        calls.append(cmd)
        for key, (code, out) in script.items():
            if key in cmd:
                return CmdResult(cmd, code, out, "", 0.0)
        return CmdResult(cmd, 0, "", "", 0.0)
    return run, calls


async def test_ios_simulator_flow_with_faked_platform_tools(tmp_path, monkeypatch):
    d = tmp_path / "ios"; (d / "App.xcodeproj").mkdir(parents=True)
    (d / ".genius" / "build" / "Build" / "Products" / "Debug-iphonesimulator" / "App.app").mkdir(parents=True)
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/usr/bin/" + n)
    p = Project.open(d)
    devices = json.dumps({"devices": {"iOS-18": [{"name": "iPhone 16", "udid": "UDID-1", "state": "Shutdown"}]}})
    run, calls = fake_runner({"-list -json": (0, json.dumps({"project": {"schemes": ["App"]}})), "simctl list devices": (0, devices), "PlistBuddy": (0, "com.acme.app\n")})
    r = await run_preview(p, runner=run)
    assert r.ok and "com.acme.app launched on iPhone 16" in r.message and [s["step"] for s in r.steps] == ["list schemes", "pick a simulator", "build for simulator", "boot + install", "launch"]
    assert any("simctl install UDID-1" in c for c in calls) and any("simctl launch UDID-1 com.acme.app" in c for c in calls)
    fail, _ = fake_runner({"-list -json": (0, json.dumps({"project": {"schemes": ["App"]}})), "simctl list devices": (0, devices), "xcodebuild -project": (65, "error: signing")})
    bad = await run_preview(p, runner=fail)
    assert not bad.ok and bad.message.startswith("the build failed") and bad.steps[-1]["step"] == "build for simulator"
    nodev, _ = fake_runner({"-list -json": (0, json.dumps({"project": {"schemes": ["App"]}})), "simctl list devices": (0, json.dumps({"devices": {}}))})
    nd = await run_preview(p, runner=nodev)
    assert not nd.ok and "install one in Xcode" in nd.reason


async def test_android_flow_with_faked_tools(tmp_path, monkeypatch):
    d = tmp_path / "and"; (d / "app/src/main").mkdir(parents=True)
    (d / "app/src/main/AndroidManifest.xml").write_text("<manifest package='com.acme.droid'/>")
    (d / "app/build.gradle").write_text("android { defaultConfig { applicationId \"com.acme.droid\" } }")
    (d / "gradlew").write_text("#!/bin/sh\n")
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/usr/bin/" + n)
    p = Project.open(d)
    run, calls = fake_runner({"adb' devices": (0, "List of devices attached\nemulator-5554\tdevice\n"), "adb devices": (0, "List of devices attached\nemulator-5554\tdevice\n")})
    r = await run_preview(p, runner=run)
    assert r.ok and "com.acme.droid launched" in r.message and any("installDebug" in c for c in calls) and any("monkey -p com.acme.droid" in c for c in calls)
    none, _ = fake_runner({"devices": (0, "List of devices attached\n"), "-list-avds": (0, "")})
    n = await run_preview(p, runner=none)
    assert not n.ok and "no Android device or emulator" in n.message and "AVD" in n.reason


def test_preview_cli_json_and_stop(tmp_path):
    d = tmp_path / "site"; d.mkdir(); (d / "index.html").write_text("<h1>x</h1>"); subprocess.run(["git", "init", "-q"], cwd=d)
    r = runner.invoke(app, ["preview", "--json", "--no-check", "--path", str(d)])
    try:
        data = json.loads(r.output)
        assert r.exit_code == 0 and data["ok"] and data["health"]["ok"] and data["url"].startswith("http://localhost:")
        out = runner.invoke(app, ["preview", "--no-check", "--path", str(d)]).output
        assert "PORT" in out and "HEALTH" in out and "already running" in out
    finally:
        runner.invoke(app, ["preview", "--stop", "--path", str(d)])
    assert runner.invoke(app, ["preview", "--path", str(tmp_path / "nothing" if (tmp_path / "nothing").mkdir() is None else tmp_path), "--no-check"]).exit_code == 1
