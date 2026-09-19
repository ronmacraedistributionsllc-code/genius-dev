"""Dev tool: render TUI screens headlessly to PNG for visual inspection.
usage: python scripts/tui_shot.py OUTDIR [WxH] [view ...]"""
import asyncio, os, shutil, subprocess, sys, tempfile
from pathlib import Path

os.environ["GENIUS_HOME"] = tempfile.mkdtemp()
import genius_dev
from genius_dev.config import PRESETS, ProviderConfig
from genius_dev.runtime import build_runtime
from genius_dev.tui.app import GeniusApp


async def main(out: Path, size: tuple[int, int], views: list[str], run_goal: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp()) / "calculator-demo"
    shutil.copytree(Path(genius_dev.__file__).parent / "demo_project", d)
    subprocess.run(["git", "init", "-q"], cwd=d)
    rt = build_runtime(d, permission="autonomous")
    rt.project.cfg.save_provider(ProviderConfig.from_dict("mock", PRESETS["mock"]), "project")
    rt.project.cfg.save_provider(ProviderConfig.from_dict("mock-b", {**PRESETS["mock"], "tier": 3}), "project")
    rt.router.refresh()
    app = GeniusApp(rt)
    svgs = {}
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.8)
        svgs["home-idle"] = app.export_screenshot()
        if run_goal:
            await pilot.press(*"Fix the failing tests in the calculator", "enter")
            await pilot.pause(0.35)
            svgs["home-running"] = app.export_screenshot()
            while app.ctl.busy:
                await pilot.pause(0.2)
            await pilot.pause(0.6)
            svgs["home-done"] = app.export_screenshot()
        for v in views:
            if v == "doctor":
                await app.run_doctor_view()
            app.set_view(v)
            await pilot.pause(0.5)
            svgs[v] = app.export_screenshot()
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        page = await b.new_page(viewport={"width": 1500, "height": 900})
        for name, svg in svgs.items():
            await page.set_content(f"<body style='margin:0;background:#0d0e11'>{svg}</body>")
            el = await page.query_selector("svg")
            await el.screenshot(path=str(out / f"{name}-{size[0]}x{size[1]}.png"))
        await b.close()
    print("wrote", ", ".join(svgs))

if __name__ == "__main__":
    out = Path(sys.argv[1])
    size = tuple(int(x) for x in sys.argv[2].split("x")) if len(sys.argv) > 2 else (120, 36)
    views = sys.argv[3:]
    asyncio.run(main(out, size, [v for v in views if v != "run"], "run" in views))
