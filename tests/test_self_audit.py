"""Genius Dev audits itself: no dead commands, dead palette items, unreachable views, placeholder code, or doc drift."""
import ast
import re
from pathlib import Path

from typer.main import get_command

from genius_dev.cli import app
from genius_dev.config import CATALOG, DEFAULT_CONFIG, PRESETS
from genius_dev.tui.app import GeniusApp
from genius_dev.tui.views import VIEW_FUNCS, VIEW_TITLES, settings_items

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "genius_dev"


def cli_names() -> set[str]:
    cmd = get_command(app)
    names = set(cmd.commands)
    for n, c in cmd.commands.items():
        if hasattr(c, "commands"):
            names |= {f"{n} {sub}" for sub in c.commands}
    return names


def test_every_command_documented_in_readme_and_docs_exists():
    names = cli_names()
    top = {n.split()[0] for n in names}
    text = "\n".join((ROOT / f).read_text() for f in ("README.md", "PROVIDERS.md", "DEVELOPMENT.md", "TESTING.md", "SECURITY.md", "PERMISSIONS.md", "MODEL_ROUTING.md"))
    missing = []
    for m in re.finditer(r"genius ([a-z][a-z-]+)(?: ([a-z][a-z-]+))?", text):
        first, second = m.group(1), m.group(2)
        if first in ("dev", "in", "is", "and", "or", "the", "a", "with", "for", "does", "can", "never", "on", "to", "config", "sets"):    # prose, not commands
            continue
        if first not in top:
            missing.append(m.group(0))
        elif first == "models" and second and second in {"add", "key", "test", "remove-key", "select-model", "set-primary", "set-fallback", "configure", "list-models", "remove"} and f"models {second}" not in names:
            missing.append(m.group(0))
    assert not missing, sorted(set(missing))


def test_every_advertised_command_has_help_and_the_required_set_exists():
    required = """new open resume status plan run finish doctor test build preview logs diff undo checkpoint checkpoints restore models model budget costs tasks
                  requirements handoff review debate security context clean config help processes stop restart protect search demo debug plugins init tui""".split()
    names = cli_names()
    assert set(required) <= names, set(required) - names
    for sub in ("models add", "models key", "models test", "models remove-key", "models select-model", "models set-primary", "models set-fallback", "models configure", "models list-models", "models remove"):
        assert sub in names, sub
    cmd = get_command(app)
    for n, c in cmd.commands.items():
        assert (c.help or "").strip() or n == "models", f"{n} has no help text"


def test_shell_completion_scripts_generate_and_complete_names(genius_home):
    from typer._completion_shared import get_completion_script
    for shell, needle in (("zsh", "compdef"), ("bash", "complete"), ("fish", "complete")):
        script = get_completion_script(prog_name="genius", complete_var="_GENIUS_COMPLETE", shell=shell)
        assert needle in script and "_GENIUS_COMPLETE" in script, shell
    from genius_dev.cli import _complete_preset, _complete_provider
    assert _complete_preset("ant") == ["anthropic"] and "openai" in _complete_preset("o")
    from genius_dev.config import Config, ProviderConfig, PRESETS
    Config(None).save_provider(ProviderConfig.from_dict("qwen", PRESETS["qwen"]))
    assert _complete_provider("q") == ["qwen"]


async def test_every_palette_item_is_live(demo_rt):
    app_ = GeniusApp(demo_rt)
    async with app_.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.6)
        items = app_.palette_items()
        titles = [t for t, _, _ in items]
        assert len(titles) == len(set(titles)), "duplicate palette titles"
        assert all(callable(cb) for _, _, cb in items)
        for view in VIEW_FUNCS:
            assert f"Go to {VIEW_TITLES[view]}" in titles, view
        for needle in ("Resume last session", "Run tests", "Run build", "Doctor", "Finish audit", "Review changes", "Security scan", "Create checkpoint", "Write handoff", "Start preview", "Stop preview", "Test models"):
            assert needle in titles, needle


def test_every_view_has_title_and_renderer():
    assert set(VIEW_FUNCS) == set(VIEW_TITLES)
    for needed in ("home", "tasks", "requirements", "models", "tools", "tests", "preview", "diff", "logs", "checkpoints", "cost", "project", "settings", "doctor"):
        assert needed in VIEW_FUNCS


def test_every_intent_kind_is_handled_by_the_controller():
    kinds = set(re.findall(r'Intent\("([a-z_]+)"', (SRC / "intent.py").read_text()))
    ctl = (SRC / "controller.py").read_text()
    for k in kinds - {"empty"}:
        assert f'k == "{k}"' in ctl, f"intent '{k}' is produced but never dispatched"


async def test_every_setting_maps_to_a_real_config_key(demo_rt):
    app_ = GeniusApp(demo_rt)
    async with app_.run_test(size=(120, 36)):
        for sec, key, label, kind, opts in settings_items(app_):
            root = key.split(".")[0]
            assert root in DEFAULT_CONFIG, key
            assert kind in ("bool", "number", "choice") and label
            if key.startswith(("providers.", "routing.custom", "general.primary")):
                continue
            cur = DEFAULT_CONFIG
            for part in key.split("."):
                assert part in cur, key
                cur = cur[part] if isinstance(cur, dict) else None


def test_provider_catalog_is_complete_and_presets_are_consistent():
    assert CATALOG == ["anthropic", "qwen", "openai", "deepseek", "gemini", "openrouter", "ollama", "lmstudio", "mock"]
    for n in CATALOG:
        p = PRESETS[n]
        assert p["kind"] in ("anthropic", "openai", "gemini", "openrouter", "ollama", "lmstudio", "mock")
        assert p["api_key_ref"] == "none" or p["api_key_ref"].startswith("env:")            # presets never carry a literal key


def test_all_tools_are_documented_and_schema_complete(demo_rt):
    from genius_dev.tools import ToolBox
    tb = ToolBox(demo_rt.project, demo_rt.perms, demo_rt.bus)
    doc = (ROOT / "TOOLS.md").read_text()
    for name, spec in tb.specs.items():
        assert spec.description and spec.schema()["parameters"]["type"] == "object"
        if name not in ("python_env", "npm_scripts", "git_summary", "browser_available", "docker_ps"):        # plugin tools are documented in PLUGINS.md
            assert name in doc, f"tool {name} missing from TOOLS.md"
    plug = (ROOT / "PLUGINS.md").read_text()
    for t in ("python_env", "npm_scripts", "git_summary", "browser_available", "docker_ps"):
        assert t in plug


def test_no_placeholder_or_unimplemented_code_in_the_source_tree():
    offenders = []
    for f in SRC.rglob("*.py"):
        if any(part.startswith("demo_") for part in f.parts):
            continue
        tree = ast.parse(f.read_text())
        text = f.read_text()
        for n in ast.walk(tree):
            if isinstance(n, ast.Raise) and "NotImplementedError" in ast.dump(n):
                offenders.append(f"{f.name}:{n.lineno} raises NotImplementedError")
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", line) and "genius:ignore" not in line:
                offenders.append(f"{f.name}:{i} {line.strip()[:60]}")
    assert not offenders, offenders


def test_docs_exist_and_status_document_has_required_sections():
    for f in ("README", "ARCHITECTURE", "PROVIDERS", "TOOLS", "PERMISSIONS", "MEMORY", "MODEL_ROUTING", "TESTING", "SECURITY", "DEVELOPMENT", "PROJECT_STATUS", "PLUGINS"):
        assert (ROOT / f"{f}.md").stat().st_size > 400, f
    status = (ROOT / "PROJECT_STATUS.md").read_text()
    for section in ("COMPLETED", "TESTED", "KNOWN ISSUES", "EXTERNAL", "NOT IMPLEMENTED", "NEXT WORK"):
        assert section in status, section
    prov = (ROOT / "PROVIDERS.md").read_text()
    assert "READY FOR KEY" in prov and "LIVE VERIFIED" in prov and "Implementation" in prov


def test_repo_contains_no_secrets():
    from genius_dev.secrets import find_secrets
    bad = []
    for f in list(ROOT.glob("*.md")) + list(SRC.rglob("*.py")) + list((ROOT / "tests").glob("*.py")) + [ROOT / "pyproject.toml"]:
        for kind in find_secrets(f.read_text(errors="replace")):
            if kind == "assignment":
                continue                                                # test fixtures assign dummy values on purpose
            if f.name.startswith("test_") or f.name == "conftest.py":
                continue
            bad.append((f.name, kind))
    assert not bad, bad
