"""Playwright integration: scripted E2E flow (signup → logout → login → create → reload → verify), audits and visual QA."""
import asyncio
import functools
import http.server
import socket
import threading

import pytest

pytest.importorskip("playwright")
from genius_dev.browser import browser_ready, run_flow  # noqa: E402
from genius_dev.tools import ToolBox  # noqa: E402
from genius_dev.visual import visual_qa  # noqa: E402

APP = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Records</title>
<style>body{font-family:system-ui;margin:0;padding:24px;color:#111;background:#fff} button,input{font-size:16px;padding:10px 14px;min-height:44px} .row{display:flex;gap:8px;margin:8px 0;flex-wrap:wrap}</style></head>
<body><main>
<section id="auth"><h1>Sign in</h1>
 <div class="row"><label for="u">User</label><input id="u"><label for="p">Password</label><input id="p" type="password"></div>
 <div class="row"><button id="signup">Sign up</button><button id="login">Log in</button></div><p id="msg" role="alert"></p></section>
<section id="app" hidden><h1>Dashboard for <span id="who"></span></h1>
 <div class="row"><label for="rec">Record</label><input id="rec"><button id="add">Add record</button><button id="logout">Log out</button></div><ul id="list"></ul></section>
</main>
<script>
const db = () => JSON.parse(localStorage.getItem('db') || '{"users":{},"records":{}}');
const save = d => localStorage.setItem('db', JSON.stringify(d));
const show = () => { const s = localStorage.getItem('session'); document.getElementById('auth').hidden = !!s; document.getElementById('app').hidden = !s;
  if (s) { document.getElementById('who').textContent = s; const d = db(); document.getElementById('list').innerHTML = (d.records[s] || []).map(r => '<li>' + r.replace(/</g,'&lt;') + '</li>').join(''); } };
document.getElementById('signup').onclick = () => { const d = db(), u = u_.value; if (d.users[u]) { msg.textContent = 'exists'; return; } d.users[u] = p_.value; save(d); localStorage.setItem('session', u); show(); };
document.getElementById('login').onclick = () => { const d = db(), u = u_.value; if (d.users[u] !== p_.value) { msg.textContent = 'Invalid credentials'; return; } localStorage.setItem('session', u); show(); };
document.getElementById('logout').onclick = () => { localStorage.removeItem('session'); show(); };
document.getElementById('add').onclick = () => { const d = db(), s = localStorage.getItem('session'); (d.records[s] = d.records[s] || []).push(rec.value); save(d); rec.value=''; show(); };
const u_ = document.getElementById('u'), p_ = document.getElementById('p'), msg = document.getElementById('msg'), rec = document.getElementById('rec');
show();
</script></body></html>"""

BAD = """<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Bad</title></head><body style="margin:0">
<h1 style="color:#ccc">Low contrast heading</h1><div style="width:900px;background:#eee">fixed 900px box</div><button></button><img src="/missing.png"><a href="#">x</a>
<script>console.error("page exploded")</script></body></html>"""


@pytest.fixture(scope="module")
def chromium():
    ok, why = asyncio.run(browser_ready())
    if not ok:
        pytest.skip(why)


def serve(directory):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


@pytest.fixture
def site(tmp_path, chromium):
    (tmp_path / "index.html").write_text(APP)
    (tmp_path / "bad.html").write_text(BAD)
    srv, url = serve(tmp_path)
    yield url
    srv.shutdown()


async def test_full_user_journey_persists_across_logout_and_reload(site, tmp_path):
    steps = [
        {"action": "fill", "selector": "#u", "value": "ada"}, {"action": "fill", "selector": "#p", "value": "pw1"}, {"action": "click", "selector": "#signup"},
        {"action": "expect_text", "selector": "#who", "value": "ada"}, {"action": "click", "selector": "#logout"}, {"action": "expect_text", "selector": "h1", "value": "Sign in"},
        {"action": "fill", "selector": "#u", "value": "ada"}, {"action": "fill", "selector": "#p", "value": "pw1"}, {"action": "click", "selector": "#login"},
        {"action": "fill", "selector": "#rec", "value": "first record"}, {"action": "click", "selector": "#add"}, {"action": "expect_text", "selector": "#list", "value": "first record"},
        {"action": "reload"}, {"action": "expect_text", "selector": "#list", "value": "first record"}, {"action": "screenshot"},
    ]
    r = await run_flow(site + "/index.html", steps, tmp_path / "shots", tag="journey")
    assert r.ok, r.error
    assert r.console_errors == [] and r.failed_requests == []
    assert len(r.log) == len(steps) + 1 and all((tmp_path / "shots").glob("*.png"))
    assert all(__import__("pathlib").Path(s).stat().st_size > 1000 for s in r.screenshots)


async def test_wrong_password_flow_and_failed_expectation_are_reported(site, tmp_path):
    r = await run_flow(site + "/index.html", [{"action": "fill", "selector": "#u", "value": "nobody"}, {"action": "fill", "selector": "#p", "value": "x"},
                                              {"action": "click", "selector": "#login"}, {"action": "expect_text", "selector": "#msg", "value": "Invalid credentials"}], tmp_path / "s")
    assert r.ok
    r = await run_flow(site + "/index.html", [{"action": "expect_text", "selector": "body", "value": "THIS TEXT DOES NOT EXIST"}], tmp_path / "s")
    assert not r.ok and "AssertionError" in r.error and any("failure" in s for s in r.screenshots)


async def test_audit_finds_real_problems_and_console_and_network_errors(site, tmp_path):
    r = await run_flow(site + "/bad.html", [], tmp_path / "s", viewport="mobile", audit=True)
    kinds = {i["kind"] for i in r.issues}
    assert {"overflow", "a11y", "contrast", "dead-link"} <= kinds
    assert any("page exploded" in c for c in r.console_errors) and any("missing.png" in f for f in r.failed_requests)
    assert not r.ok


async def test_audit_passes_a_clean_page_on_both_viewports(site, tmp_path):
    for vp in ("desktop", "mobile"):
        r = await run_flow(site + "/index.html", [], tmp_path / "s", viewport=vp, audit=True)
        assert r.ok and not [i for i in r.issues if i["sev"] == "fail"], (vp, r.issues)


async def test_browser_tool_and_visual_qa_end_to_end(tmp_path, make_rt, chromium):
    d = tmp_path / "web"; d.mkdir()
    (d / "index.html").write_text(BAD)
    import subprocess; subprocess.run(["git", "init", "-q"], cwd=d)
    rt = make_rt(d)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    import sys, shlex
    rt.project.info.dev_cmd, rt.project.info.dev_port = f"{shlex.quote(sys.executable)} -m http.server {port} --bind 127.0.0.1", port
    tools = ToolBox(rt.project, rt.perms, rt.bus)
    try:
        rep = await visual_qa(rt.project, tools, rt.router, rt.bus)
    finally:
        rt.project.procs.stop_all()
    assert rep.ran and not rep.ok and rep.screenshots and rep.url.endswith(str(port))
    text = rep.text()
    assert "UI REVIEW" in text and "FAILURES" in text and any("overflow" in f for f in rep.failures)
    assert rep.console_errors
    assert not any(p["status"] == "running" for p in rt.project.procs.list())          # cleaned up


async def test_visual_qa_reports_why_it_could_not_run(tmp_path, make_rt):
    d = tmp_path / "nodev"; d.mkdir(); (d / "a.py").write_text("x=1\n")
    rt = make_rt(d)
    rep = await visual_qa(rt.project, ToolBox(rt.project, rt.perms, rt.bus), rt.router, rt.bus)
    assert not rep.ran and "dev server" in rep.reason


async def test_axe_core_findings_are_merged_when_installed(site, tmp_path):
    from genius_dev.browser import axe_installed
    if not axe_installed():
        pytest.skip("axe-playwright-python not installed (optional extra: genius-dev[a11y])")
    r = await run_flow(site + "/bad.html", [], tmp_path / "s", viewport="desktop", audit=True)
    assert any(i["kind"] == "axe:button-name" and i["sev"] == "fail" for i in r.issues)
    clean = await run_flow(site + "/index.html", [], tmp_path / "s", viewport="desktop", audit=True)
    assert not [i for i in clean.issues if i["sev"] == "fail"]


async def test_tablet_viewport_and_empty_space_heuristic(tmp_path):
    (tmp_path / "e.html").write_text("<!doctype html><meta name=viewport content='width=device-width'><title>t</title><p>tiny</p>")
    srv, url = serve(tmp_path)
    try:
        r = await run_flow(url + "/e.html", [], tmp_path / "s", viewport="tablet", audit=True)
    finally:
        srv.shutdown()
    assert any(i["kind"] == "empty-space" for i in r.issues)
