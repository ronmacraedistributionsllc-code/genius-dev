"""genius preview — start the app the best available way and report exactly what happened (or exactly why it can't)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .plugins import PreviewPlan, load_registry
from .runner import CmdResult, run_command

RunFn = Callable[[str, Path, float], Awaitable[CmdResult]]


@dataclass
class PreviewResult:
    ok: bool
    kind: str
    title: str = ""
    url: str = ""
    port: int = 0
    process: str = ""
    pid: int = 0
    log: str = ""
    status: str = ""
    health: dict[str, Any] = field(default_factory=dict)
    browser: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    reason: str = ""                       # populated when the platform cannot be previewed
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _sim_tools(project) -> tuple[bool, str]:
    if not shutil.which("xcrun"):
        return False, "Xcode command-line tools are not installed (xcrun not found)"
    return True, ""


def toolchain_preview(project) -> PreviewPlan | None:
    root = project.root
    xc = list(root.glob("*.xcodeproj")) + list(root.glob("*.xcworkspace"))
    if xc:
        ok, why = _sim_tools(project)
        return PreviewPlan("ios-simulator", "iOS app in the Simulator", steps=["list schemes", "pick a simulator", "build for simulator", "boot + install", "launch"],
                           available=ok, reason=why, plugin="toolchains", command=xc[0].name)
    if (root / "app/src/main/AndroidManifest.xml").exists():
        adb = shutil.which("adb") or _sdk_tool("platform-tools/adb")
        if not adb:
            return PreviewPlan("android-emulator", "Android app", available=False, plugin="toolchains", reason="adb not found — install the Android SDK platform-tools and set ANDROID_HOME")
        return PreviewPlan("android-emulator", "Android app on an emulator", steps=["find/boot an emulator", "gradle installDebug", "launch"], plugin="toolchains", command=adb)
    if (root / "pubspec.yaml").exists():
        if not shutil.which("flutter"):
            return PreviewPlan("web-server", "Flutter (web)", available=False, plugin="toolchains", reason="flutter is not installed")
        return PreviewPlan("web-server", "Flutter (Chrome)", "flutter run -d web-server --web-port {port}", _free(8090), plugin="toolchains", notes=["serves Flutter web; open the URL in a browser"])
    if (root / "Cargo.toml").exists():
        return PreviewPlan("cli", "Rust binary", "cargo run --quiet", plugin="toolchains", notes=["CLI: runs once and shows its output"]) if shutil.which("cargo") else PreviewPlan("cli", "Rust binary", available=False, reason="cargo is not installed", plugin="toolchains")
    if (root / "go.mod").exists():
        return PreviewPlan("cli", "Go program", "go run .", plugin="toolchains", notes=["CLI: runs once and shows its output"]) if shutil.which("go") else PreviewPlan("cli", "Go program", available=False, reason="go is not installed", plugin="toolchains")
    if (root / "Package.swift").exists():
        return PreviewPlan("cli", "Swift package", "swift run", plugin="toolchains", notes=["CLI: runs once and shows its output"]) if shutil.which("swift") else PreviewPlan("cli", "Swift package", available=False, reason="swift is not installed", plugin="toolchains")
    return None


def _sdk_tool(rel: str) -> str | None:
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    p = Path(sdk) / rel if sdk else None
    return str(p) if p and p.exists() else None


def _free(start: int) -> int:
    from .detect import _free_port
    return _free_port(start)


def choose_plan(project) -> PreviewPlan:
    info = project.info
    reg = load_registry(project.root)
    for pl in reg.plugins:                                              # plugins get first say (Electron, compose, simulators …)
        try:
            plan = pl.preview(project)
        except Exception as e:  # noqa: BLE001
            reg.errors.append(f"{pl.name}.preview failed: {e}")
            continue
        if plan:                                                        # first plugin with an opinion wins; built-ins only speak when they are the right tool
            return plan
    if info.dev_cmd:
        return PreviewPlan("web-server", f"{', '.join(info.frameworks) or info.kind} dev server", info.dev_cmd, info.dev_port, plugin="core")
    return PreviewPlan("none", "Nothing to preview", available=False, plugin="core",
                       reason="no dev-server command, simulator project, or runnable entry point was detected. Tell the agent how the app starts, or set it in .genius/config.toml")


async def _health(url: str, timeout: float = 20) -> dict[str, Any]:
    import httpx
    end, last = time.time() + timeout, ""
    async with httpx.AsyncClient(timeout=3, follow_redirects=True) as c:
        while time.time() < end:
            t = time.perf_counter()
            try:
                r = await c.get(url)
                return {"ok": r.status_code < 500, "status": r.status_code, "ms": round((time.perf_counter() - t) * 1000)}
            except Exception as e:  # noqa: BLE001
                last = type(e).__name__
                await asyncio.sleep(0.4)
    return {"ok": False, "status": 0, "ms": 0, "error": last or "no response"}


async def run_preview(project, open_browser: bool = False, check: bool = True, runner: RunFn | None = None, ready_timeout: float = 30, health_timeout: float = 20) -> PreviewResult:
    plan = choose_plan(project)
    if not plan.available:
        return PreviewResult(False, plan.kind, plan.title, reason=plan.reason, message=f"Cannot preview: {plan.reason}", notes=plan.notes)
    run: RunFn = runner or (lambda c, cwd, t: run_command(c, cwd, t))
    if plan.kind == "ios-simulator":
        return await _ios(project, plan, run)
    if plan.kind == "android-emulator":
        return await _android(project, plan, run)
    if plan.kind == "cli":
        r = await run(plan.command, project.root, 60)
        return PreviewResult(r.ok, "cli", plan.title, message=f"exit {r.code}", steps=[{"step": plan.command, "ok": r.ok, "detail": r.output[-600:]}], notes=plan.notes)
    name = "compose" if plan.kind == "compose" else "dev"
    port = plan.port
    cmd = plan.command.replace("{port}", str(port))
    try:
        proc = await project.procs.start(name, cmd, port, ready_timeout)
    except RuntimeError as e:
        msg = str(e)
        hint = "another process already uses that port — stop it or change the dev-server port" if "already in use" in msg else "see the log tail above"
        return PreviewResult(False, plan.kind, plan.title, process=name, port=port, message=msg.splitlines()[0], reason=hint, notes=[msg])
    port = proc.get("port") or port
    url = proc.get("url") or (f"http://localhost:{port}" if port else "")
    res = PreviewResult(True, plan.kind, plan.title, url, port, name, proc.get("pid", 0), proc.get("log", ""), proc.get("status", ""), notes=plan.notes)
    res.notes.append("already running (reused)" if proc.get("reused") else "started")
    if plan.kind in ("web-server", "compose") and url:
        res.health = await _health(url, health_timeout)
        if not res.health["ok"]:
            res.ok, res.message = False, f"server started but {url} did not answer healthily ({res.health.get('error') or res.health.get('status')}). Log tail:\n" + project.procs.tail(name, 12)
            return res
        if check:
            res.browser = await _browser_check(project, url)
        if open_browser:
            webbrowser.open(url)
            res.notes.append("opened in your default browser")
    elif plan.kind == "desktop-app":
        await asyncio.sleep(2)
        cur = project.procs.get(name)
        res.ok = bool(cur and cur["status"] == "running")
        res.status = cur["status"] if cur else "unknown"
        res.message = "app launched" if res.ok else "the app exited right away. Log tail:\n" + project.procs.tail(name, 12)
    return res


async def _browser_check(project, url: str) -> dict[str, Any]:
    from .browser import browser_ready, run_flow
    ok, why = await browser_ready()
    if not ok:
        return {"ran": False, "reason": why}
    r = await run_flow(url, [], project.gdir / "screenshots", bool(project.cfg.get("browser.headless", True)), "desktop", False, tag="preview")
    return {"ran": True, "ok": r.ok and not r.console_errors, "console_errors": r.console_errors[:8], "failed_requests": r.failed_requests[:8],
            "screenshot": r.screenshots[-1] if r.screenshots else "", "error": r.error}


# ---------------------------------------------------------------------------------------------- simulators
async def _ios(project, plan: PreviewPlan, run: RunFn) -> PreviewResult:
    root = project.root
    res = PreviewResult(False, "ios-simulator", plan.title)
    step = lambda name, ok, detail="": res.steps.append({"step": name, "ok": ok, "detail": detail[-400:]})
    flag = "-workspace" if plan.command.endswith(".xcworkspace") else "-project"
    r = await run(f"xcodebuild {flag} {shlex.quote(plan.command)} -list -json", root, 60)
    try:
        data = json.loads(r.stdout[r.stdout.index("{"):])
        info = data.get("project") or data.get("workspace") or {}
        schemes = info.get("schemes", [])
    except Exception:  # noqa: BLE001
        schemes = []
    step("list schemes", bool(schemes), ", ".join(schemes) or r.output)
    if not schemes:
        res.message, res.reason = "no scheme found in the Xcode project", "open the project in Xcode once so a shared scheme is created"
        return res
    scheme = schemes[0]
    r = await run("xcrun simctl list devices available -j", root, 30)
    try:
        devs = [d for lst in json.loads(r.stdout)["devices"].values() for d in lst if "iPhone" in d["name"]]
    except Exception:  # noqa: BLE001
        devs = []
    step("pick a simulator", bool(devs), devs[0]["name"] if devs else "no available iPhone simulators")
    if not devs:
        res.message, res.reason = "no iPhone simulator runtime is installed", "install one in Xcode → Settings → Platforms"
        return res
    dev = next((d for d in devs if d.get("state") == "Booted"), devs[0])
    derived = root / ".genius" / "build"
    r = await run(f"xcodebuild {flag} {shlex.quote(plan.command)} -scheme {shlex.quote(scheme)} -destination 'platform=iOS Simulator,id={dev['udid']}' -derivedDataPath {shlex.quote(str(derived))} build", root, 1200)
    step("build for simulator", r.ok, r.output[-300:])
    if not r.ok:
        res.message = "the build failed — see the step detail"
        return res
    apps = sorted((derived / "Build" / "Products").glob("*-iphonesimulator/*.app"))
    if not apps:
        step("locate .app", False, "no .app produced")
        res.message = "build succeeded but produced no .app bundle"
        return res
    app = apps[0]
    b = await run(f"/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' {shlex.quote(str(app / 'Info.plist'))}", root, 10)
    bundle = b.stdout.strip()
    await run(f"xcrun simctl boot {dev['udid']}", root, 60)                 # already-booted is fine
    await run("open -a Simulator", root, 20)
    i = await run(f"xcrun simctl install {dev['udid']} {shlex.quote(str(app))}", root, 120)
    step("boot + install", i.ok, i.output[-200:])
    if not i.ok:
        res.message = "install failed"
        return res
    l = await run(f"xcrun simctl launch {dev['udid']} {shlex.quote(bundle)}", root, 30)
    step("launch", l.ok, l.output[-200:])
    res.ok, res.message = l.ok, f"{bundle} launched on {dev['name']}" if l.ok else "launch failed"
    return res


async def _android(project, plan: PreviewPlan, run: RunFn) -> PreviewResult:
    root = project.root
    res = PreviewResult(False, "android-emulator", plan.title)
    step = lambda name, ok, detail="": res.steps.append({"step": name, "ok": ok, "detail": detail[-400:]})
    adb = plan.command
    r = await run(f"{shlex.quote(adb)} devices", root, 20)
    online = [l for l in r.stdout.splitlines()[1:] if l.strip().endswith("device")]
    if not online:
        emu = shutil.which("emulator") or _sdk_tool("emulator/emulator")
        avds = (await run(f"{shlex.quote(emu)} -list-avds", root, 20)).stdout.split() if emu else []
        if not avds:
            step("find/boot an emulator", False, "no device connected and no AVD defined")
            res.message, res.reason = "no Android device or emulator available", "create an AVD in Android Studio (Device Manager) or connect a device with USB debugging"
            return res
        await run(f"nohup {shlex.quote(emu or 'emulator')} -avd {avds[0]} -no-snapshot >/dev/null 2>&1 &", root, 5)
        w = await run(f"{shlex.quote(adb)} wait-for-device", root, 180)
        step("find/boot an emulator", w.ok, avds[0])
    else:
        step("find/boot an emulator", True, online[0].split()[0])
    gradle = "./gradlew" if (root / "gradlew").exists() else "gradle"
    g = await run(f"{gradle} installDebug", root, 1200)
    step("gradle installDebug", g.ok, g.output[-300:])
    if not g.ok:
        res.message = "install failed"
        return res
    pkg = ""
    for f in (root / "app/build.gradle", root / "app/build.gradle.kts"):
        if f.exists() and (m := re.search(r"applicationId\s*=?\s*[\"']([\w.]+)[\"']", f.read_text())):
            pkg = m.group(1)
    if not pkg:
        m = re.search(r'package="([\w.]+)"', (root / "app/src/main/AndroidManifest.xml").read_text())
        pkg = m.group(1) if m else ""
    l = await run(f"{shlex.quote(adb)} shell monkey -p {pkg} -c android.intent.category.LAUNCHER 1", root, 30)
    step("launch", l.ok and bool(pkg), pkg or "could not determine the application id")
    res.ok, res.message = l.ok and bool(pkg), f"{pkg} launched" if pkg and l.ok else "launch failed"
    return res
