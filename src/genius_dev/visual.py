"""Visual QA: run the app, audit each viewport with deterministic DOM checks, optionally add a vision-model critique."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .browser import browser_ready, encode_png, run_flow
from .events import EventBus
from .project import Project

UI_REVIEW_SYSTEM = """[[role:ui_reviewer]]
You are a meticulous product designer reviewing screenshots of a web app. Look for: clipped text, overlap, bad spacing,
weak typography and hierarchy, giant empty areas, misaligned controls, inconsistent components, low contrast, generic boxy
design, broken responsive layout. Reply as JSON: {"verdict":"pass|warn|fail","issues":[{"severity":"fail|warn","where":"","problem":"","fix":""}],"summary":""}"""


@dataclass
class VisualReport:
    ran: bool = False
    reason: str = ""
    url: str = ""
    passes: int = 0
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    model_review: str = ""

    @property
    def ok(self) -> bool:
        return self.ran and not self.failures and not self.console_errors

    def text(self) -> str:
        if not self.ran:
            return f"Visual QA not run: {self.reason}"
        lines = [f"UI REVIEW — {self.url}", f"  PASS {self.passes} · WARNINGS {len(self.warnings)} · FAILURES {len(self.failures)}"]
        lines += [f"  ✕ {f}" for f in self.failures] + [f"  ! {w}" for w in self.warnings[:12]]
        lines += [f"  console: {c}" for c in self.console_errors[:5]] + [f"  network: {c}" for c in self.failed_requests[:5]]
        if self.model_review:
            lines.append("  model: " + self.model_review[:300])
        return "\n".join(lines)


async def ensure_server(project: Project, tools) -> tuple[str, str]:
    """Start (or reuse) the dev server; return (url, error)."""
    info = project.info
    if not info.dev_cmd:
        return "", "no dev server command detected"
    r = await tools.execute("process_manager", {"action": "start", "name": "dev"})
    if not r.ok:
        return "", r.summary
    return r.data.get("url") or f"http://localhost:{info.dev_port}", ""


async def visual_qa(project: Project, tools, router, bus: EventBus, routes: list[str] | None = None) -> VisualReport:
    rep = VisualReport()
    ok, why = await browser_ready()
    if not ok:
        rep.reason = why
        return rep
    url, err = await ensure_server(project, tools)
    if err:
        rep.reason = err
        return rep
    rep.ran, rep.url = True, url
    routes = routes or ["/"] + sorted({r["route"] for r in project.index.routes() if r["method"] in ("GET", "ANY") and "{" not in r["route"] and ":" not in r["route"]})[:5]
    seen = set()
    for route in routes:
        target = url.rstrip("/") + (route if route.startswith("/") else "/" + route)
        if target in seen:
            continue
        seen.add(target)
        for vp in ("desktop", "tablet", "mobile"):
            bus.emit("BROWSER", "AUDIT", f"{route} @ {vp}")
            fr = await run_flow(target, [], project.gdir / "screenshots", bool(project.cfg.get("browser.headless", True)), vp, True, tag=route.strip("/").replace("/", "_") or "home")
            rep.screenshots += fr.screenshots
            if fr.error:
                rep.failures.append(f"{route} @ {vp}: {fr.error}")
            for i in fr.issues:
                (rep.failures if i["sev"] == "fail" else rep.warnings).append(f"{route} @ {vp}: [{i['kind']}] {i['msg']}")
            rep.console_errors += [f"{route}: {c}" for c in fr.console_errors]
            rep.failed_requests += [f"{route}: {c}" for c in fr.failed_requests]
            if not fr.issues and not fr.error:
                rep.passes += 1
    rep.console_errors, rep.failed_requests = sorted(set(rep.console_errors)), sorted(set(rep.failed_requests))
    # optional model critique (only when a vision-capable provider is configured and allowed)
    if rep.screenshots:
        route = router.route("ui_reviewer", needs_vision=True)
        if route.chain:
            try:
                shots = [s for s in rep.screenshots if "failure" not in s][-2:]
                comp = await router.complete("ui_reviewer", [{"role": "user", "content": "Review these screenshots (desktop then mobile).", "images": [encode_png(s) for s in shots]}],
                                             system=UI_REVIEW_SYSTEM, json_mode=True, images=True)
                data = json.loads(comp.text[comp.text.find("{"): comp.text.rfind("}") + 1] or "{}")
                rep.model_review = data.get("summary", "")
                for i in data.get("issues", []):
                    (rep.failures if i.get("severity") == "fail" else rep.warnings).append(f"model: {i.get('where', '')} {i.get('problem', '')}")
            except Exception as e:  # noqa: BLE001
                bus.emit("ERRORS", "MODEL", f"vision review unavailable: {str(e)[:100]}")
    return rep
