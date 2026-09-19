"""TUI tests via Textual's pilot: keyboard navigation, palette, dialogs, and layout at small / standard / large sizes."""
import asyncio

import pytest
from rich.console import Console

from genius_dev.tui.app import ApprovalScreen, GeniusApp
from genius_dev.tui.views import VIEW_FUNCS

SIZES = [(60, 18), (80, 24), (120, 36), (200, 55)]


@pytest.fixture
def app(demo_rt):
    return GeniusApp(demo_rt)


async def settle(pilot, t=0.3):
    await pilot.pause(t)


def text_of(app) -> str:
    c = Console(width=app.size.width, record=True, force_terminal=False)
    from genius_dev.tui.views import VIEW_FUNCS
    c.print(VIEW_FUNCS[app.view](app, max(40, app.query_one("#main").size.width), app.query_one("#main").size.height))
    return c.export_text()


@pytest.mark.parametrize("size", SIZES)
async def test_every_view_renders_without_clipping_at_all_sizes(app, size):
    async with app.run_test(size=size) as pilot:
        await settle(pilot, 0.5)
        w = app.query_one("#main").size.width
        for view in VIEW_FUNCS:
            app.set_view(view)
            await settle(pilot, 0.15)
            c = Console(width=max(40, w), record=True, force_terminal=False)
            c.print(VIEW_FUNCS[view](app, max(40, w), app.query_one("#main").size.height))
            lines = c.export_text().splitlines()
            assert lines, view
            assert max(len(l) for l in lines) <= max(40, w), (view, size, max(len(l) for l in lines))
        app.set_view("home")
        await settle(pilot)
        hdr = app.query_one("#hdr").render()
        assert "GENIUS DEV" in str(hdr) and all(len(l) <= app.size.width for l in str(hdr).splitlines())


async def test_startup_narrative_and_idle_state(app):
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, 1.0)
        msgs = [e.message for e in app.rt.bus.buffer]
        assert any("Repository index ready" in m for m in msgs) and msgs[-1] == "Ready." and any("connected" in m for m in msgs)
        assert "Idle" in text_of(app)


async def test_shortcuts_switch_views_and_escape_returns_home(app):
    async with app.run_test(size=(120, 36)) as pilot:
        for key, view in (("ctrl+p", "tasks"), ("ctrl+l", "logs"), ("ctrl+t", "tests"), ("ctrl+d", "diff"), ("ctrl+v", "preview"), ("ctrl+o", "models")):
            await pilot.press(key)
            await settle(pilot, 0.1)
            assert app.view == view, key
        await pilot.press("escape")
        assert app.view == "home"


async def test_command_palette_opens_filters_and_runs(app):
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press("ctrl+k")
        await settle(pilot, 0.4)
        assert type(app.screen).__name__ == "CommandPalette"
        await pilot.press(*"go to cost")
        await settle(pilot, 0.6)
        await pilot.press("enter")
        await settle(pilot, 0.5)
        assert app.view == "cost" and type(app.screen).__name__ != "CommandPalette"


async def test_palette_searches_project_files_and_symbols(app):
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, 0.6)
        await pilot.press("ctrl+k")
        await pilot.press(*"divide")
        await settle(pilot, 0.8)
        await pilot.press("enter")
        await settle(pilot, 0.5)
        assert app.view == "output" and "def divide" in app.last_result


async def test_up_down_select_and_settings_left_right_change_config(app):
    async with app.run_test(size=(120, 36)) as pilot:
        app.rt.project.reqs.add("one"); app.rt.project.reqs.add("two")
        app.set_view("requirements")
        await pilot.press("down")
        assert app.sel["requirements"] == 1
        await pilot.press("down", "down")
        assert app.sel["requirements"] == 1                   # clamped to last item
        await pilot.press("up", "up")
        assert app.sel["requirements"] == 0
        app.set_view("settings")
        app.sel["settings"] = next(i for i, it in enumerate(__import__("genius_dev.tui.views", fromlist=["x"]).settings_items(app)) if it[1] == "general.permission")
        before = app.rt.project.cfg.permission
        await pilot.press("right")
        await settle(pilot, 0.1)
        assert app.rt.project.cfg.permission != before and app.rt.perms.mode == app.rt.project.cfg.permission
        idx = next(i for i, it in enumerate(__import__("genius_dev.tui.views", fromlist=["x"]).settings_items(app)) if it[1] == "general.daily_budget")
        app.sel["settings"] = idx
        await pilot.press("right", "right")
        assert app.rt.project.cfg.get("general.daily_budget") == 1.0


async def test_typing_natural_language_runs_the_agent_and_shows_result(app):
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, 0.5)
        await pilot.press(*"Fix the failing tests in the calculator", "enter")
        await settle(pilot, 0.3)
        assert app.rt.agent.state["status"] == "running" or app.ctl.busy
        assert "Working" in app.query_one("#prompt").placeholder
        while app.ctl.busy:
            await settle(pilot, 0.2)
        await settle(pilot, 0.6)
        txt = text_of(app)
        assert "complete" in txt and "REQ-001" in txt and "100%" in txt
        assert app.ctl.last_report.success


async def test_stop_command_and_shortcut(app):
    async with app.run_test(size=(120, 36)) as pilot:
        app.rt.router.provider("mock").delay = 0.2
        await pilot.press(*"Fix the failing tests in the calculator", "enter")
        await settle(pilot, 0.6)
        await pilot.press("ctrl+x")
        while app.ctl.busy:
            await settle(pilot, 0.2)
        assert app.ctl.last_report.status == "stopped"


async def test_approval_dialog_allows_and_denies(demo_dir, make_rt):
    rt = make_rt(demo_dir, permission="safe")
    app = GeniusApp(rt)
    async with app.run_test(size=(120, 36)) as pilot:
        t = asyncio.create_task(app.ask_permission("write calculator.py", "SAFE mode: confirm write"))
        await settle(pilot, 0.3)
        assert isinstance(app.screen, ApprovalScreen)
        await pilot.press("y")
        assert await t is True
        t = asyncio.create_task(app.ask_permission("rm -rf build", "destructive"))
        await settle(pilot, 0.3)
        await pilot.press("n")
        assert await t is False
        t = asyncio.create_task(app.ask_permission("x", "y"))
        await settle(pilot, 0.3)
        await pilot.press("escape")
        assert await t is False                                    # escape denies, never allows


async def test_agent_permission_prompt_flows_through_dialog(demo_dir, make_rt):
    rt = make_rt(demo_dir, permission="safe")
    app = GeniusApp(rt)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press(*"Fix the failing tests in the calculator", "enter")
        for _ in range(200):
            await settle(pilot, 0.1)
            if isinstance(app.screen, ApprovalScreen):
                await pilot.press("y")
            if not app.ctl.busy and app.ctl.last_report:
                break
        assert app.ctl.last_report and app.ctl.last_report.success


async def test_logs_view_filter_search_expand(app):
    async with app.run_test(size=(120, 36)) as pilot:
        app.rt.bus.emit("ERRORS", "BUILD", "TypeScript error in MerchantDashboard.tsx", detail="line1\nline2")
        app.rt.bus.emit("AGENT", "NOTE", "hello agent")
        await pilot.press("ctrl+l")
        await settle(pilot, 0.2)
        assert "hello agent" in text_of(app)
        await pilot.press("8")
        await settle(pilot, 0.2)
        assert app.log_filter == "ERRORS" and "hello agent" not in text_of(app) and "MerchantDashboard" in text_of(app)
        await pilot.press("enter")
        assert "line2" in text_of(app)
        await pilot.press("1", "/", *"nothing-here")
        await settle(pilot, 0.2)
        assert app.log_query == "nothing-here" and "No matching events" in text_of(app)
        await pilot.press("escape")
        assert app.log_query == ""


async def test_key_remapping_from_config(demo_dir, make_rt):
    rt = make_rt(demo_dir)
    rt.project.cfg.set("ui.keys.logs", "ctrl+g", "project")
    app = GeniusApp(rt)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press("ctrl+g")
        assert app.view == "logs"


async def test_checkpoints_view_restore_requires_confirmation(app):
    async with app.run_test(size=(120, 36)) as pilot:
        p = app.rt.project
        p.checkpoints.create("base")
        (p.root / "calculator.py").write_text("changed\n")
        app.set_view("checkpoints")
        await pilot.press("enter")
        await settle(pilot, 0.3)
        assert isinstance(app.screen, ApprovalScreen)
        await pilot.press("n")
        await settle(pilot, 0.2)
        assert (p.root / "calculator.py").read_text() == "changed\n"
        await pilot.press("enter")
        await settle(pilot, 0.3)
        await pilot.press("y")
        await settle(pilot, 0.6)
        assert "def add" in (p.root / "calculator.py").read_text()


async def test_no_provider_configured_shows_actionable_hint(demo_dir, make_rt):
    app = GeniusApp(make_rt(demo_dir, {}))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, 0.8)
        assert any("No model configured" in e.message for e in app.rt.bus.buffer)
        app.set_view("models")
        await settle(pilot, 0.6)
        assert "READY FOR KEY" in text_of(app) and "NOT CONFIGURED" in text_of(app)


# ---------------- provider management screen ----------------
from textual.widgets import Input  # noqa: E402


async def open_models(app, pilot):
    app.set_view("models")
    await settle(pilot, 0.8)


async def test_models_screen_lists_catalog_with_honest_statuses(app, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    async with app.run_test(size=(120, 36)) as pilot:
        await open_models(app, pilot)
        txt = text_of(app)
        for label in ("Anthropic", "Qwen", "OpenAI", "DeepSeek", "Gemini", "OpenRouter", "Ollama", "LM Studio", "Mock"):
            assert label in txt, label
        assert txt.count("READY FOR KEY") >= 6 and "NOT CONFIGURED" in txt and "● CONNECTED" in txt and "genius-mock-v1" in txt
        assert "add key" in txt and "set primary" in txt and "remove key" in txt and "0 live-verified" in txt


async def test_add_key_dialog_is_hidden_stores_in_keychain_and_never_displays_secret(app, fake_keychain, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    secret = "sk-ant-SECRETVALUE-1234567890abcdef"
    async with app.run_test(size=(120, 36)) as pilot:
        await open_models(app, pilot)
        await pilot.press("a")                                          # ADD KEY on the selected row (Anthropic)
        await settle(pilot, 0.5)
        from genius_dev.tui.app import PromptScreen
        assert isinstance(app.screen, PromptScreen)
        box = app.screen.query_one("#dlg-input", Input)
        assert box.password is True
        box.value = secret
        assert "SECRETVALUE" not in app.export_screenshot()               # masked in the rendered dialog
        await pilot.press("enter")
        await settle(pilot, 0.8)
        assert fake_keychain[("genius-dev", "anthropic")] == secret
        assert "SECRETVALUE" not in app.export_screenshot() and "SECRETVALUE" not in text_of(app)
        st = {s.name: s for s in app.pstatus}["anthropic"]
        assert st.key_present and st.state == "KEY SET · UNTESTED" and not st.live_verified               # a stored key is not "connected"
        assert 'keychain:anthropic' in (app.rt.project.cfg.global_path.read_text())
        assert "SECRETVALUE" not in app.rt.project.cfg.global_path.read_text()


async def test_test_action_never_contacts_a_provider_without_a_key(app, monkeypatch):
    import httpx
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")))
    async with app.run_test(size=(120, 36)) as pilot:
        await open_models(app, pilot)
        await pilot.press("t")
        await settle(pilot, 0.5)
        assert {s.name: s for s in app.pstatus}["anthropic"].state == "READY FOR KEY"


async def test_set_primary_fallback_and_select_model_from_the_screen(app):
    async with app.run_test(size=(120, 36)) as pilot:
        await open_models(app, pilot)
        await pilot.press("down")                                       # Qwen
        await pilot.press("p")
        await settle(pilot, 0.6)
        assert app.rt.project.cfg.get("general.primary") == "qwen" and {s.name: s for s in app.pstatus}["qwen"].primary
        await pilot.press("f")
        await settle(pilot, 0.6)
        assert app.rt.project.cfg.get("fallback.chain") == ["qwen"]
        await pilot.press("m")
        await settle(pilot, 0.5)
        app.screen.query_one("#dlg-input", Input).value = "qwen-coder-test"
        await pilot.press("enter")
        await settle(pilot, 0.8)
        assert app.rt.project.cfg.providers()["qwen"].model == "qwen-coder-test"
        assert {s.name: s for s in app.pstatus}["qwen"].state == "READY FOR KEY"                       # model chosen, still no key
        await pilot.press("f")
        await settle(pilot, 0.5)
        assert app.rt.project.cfg.get("fallback.chain") == []            # toggles off


async def test_remove_key_requires_confirmation(app, fake_keychain):
    fake_keychain[("genius-dev", "qwen")] = "k" * 24
    from genius_dev.config import Config, ProviderConfig, PRESETS
    Config(None).save_provider(ProviderConfig.from_dict("qwen", {**PRESETS["qwen"], "api_key_ref": "keychain:qwen"}))
    async with app.run_test(size=(120, 36)) as pilot:
        await open_models(app, pilot)
        await pilot.press("down")
        await pilot.press("x")
        await settle(pilot, 0.4)
        assert isinstance(app.screen, ApprovalScreen)
        await pilot.press("n")
        await settle(pilot, 0.3)
        assert ("genius-dev", "qwen") in fake_keychain
        await pilot.press("x")
        await settle(pilot, 0.4)
        await pilot.press("y")
        await settle(pilot, 0.6)
        assert ("genius-dev", "qwen") not in fake_keychain


async def test_palette_exposes_provider_actions(app):
    async with app.run_test(size=(120, 36)) as pilot:
        await open_models(app, pilot)
        titles = [t for t, _, _ in app.palette_items()]
        for needle in ("Anthropic: add key", "Qwen: test", "OpenAI: select model", "DeepSeek: set primary", "Gemini: set fallback", "OpenRouter: configure", "Anthropic: remove key", "Go to Doctor"):
            assert needle in titles, needle


async def test_doctor_view_runs_real_checks(app):
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, 0.5)
        await app.run_doctor_view()
        await settle(pilot, 0.3)
        assert app.view == "doctor"
        txt = text_of(app)
        for needle in ("SYSTEM", "PROJECT", "MODELS", "QUALITY", "every check ran for real", "Mock provider", "READY FOR KEY"):
            assert needle in txt, needle


async def test_diff_view_file_drilldown(app):
    async with app.run_test(size=(120, 36)) as pilot:
        root = app.rt.project.root
        (root / "a_new.py").write_text("ALPHA = 1\n")
        (root / "b_new.py").write_text("BETA = 2\n")
        app.diff_cache = (0.0, [], "")
        app.set_view("diff")
        await settle(pilot, 0.4)
        names = [f for f, _, _ in app.diff_cache[1]]
        ia, ib = names.index("a_new.py"), names.index("b_new.py")
        app.sel["diff"] = ia
        await settle(pilot, 0.4)
        first = text_of(app)
        assert "ALPHA" in first and "BETA" not in first                  # only the selected file's diff is shown
        await pilot.press("down")
        await settle(pilot, 0.3)
        assert app.sel["diff"] == ia + 1 or ib == ia + 1
        app.sel["diff"] = ib
        await settle(pilot, 0.3)
        second = text_of(app)
        assert "BETA" in second and "ALPHA" not in second
        await pilot.press("enter")
        await settle(pilot, 0.3)
        assert app.diff_all and "ALPHA" in text_of(app) and "BETA" in text_of(app)


async def test_approval_dialog_shows_colored_change_and_scrolls(demo_dir, make_rt):
    rt = make_rt(demo_dir, permission="safe")
    app = GeniusApp(rt)
    async with app.run_test(size=(120, 36)) as pilot:
        long = "SAFE mode: confirm edit\n\n" + "\n".join(f"+line {i}" for i in range(40))
        t = asyncio.create_task(app.ask_permission("edit calculator.py", long))
        await settle(pilot, 0.4)
        assert isinstance(app.screen, ApprovalScreen)
        await pilot.press("down", "down")
        await pilot.press("y")
        assert await t is True
