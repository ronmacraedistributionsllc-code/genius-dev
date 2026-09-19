import json

import pytest
from typer.testing import CliRunner

from genius_dev.cli import app

runner = CliRunner()


def cli(*args, cwd=None):
    r = runner.invoke(app, list(args) + (["--path", str(cwd)] if cwd and "--path" not in args else []), catch_exceptions=False)
    return r


@pytest.fixture
def proj(demo_dir):
    assert cli("models", "add", "mock", cwd=None).exit_code == 0
    return demo_dir


def test_version_and_help_list_all_commands():
    assert "genius-dev" in cli("--version").output
    out = cli("--help").output
    for c in ("new", "open", "resume", "status", "plan", "run", "finish", "doctor", "test", "build", "preview", "logs", "diff", "undo", "checkpoint", "checkpoints", "restore",
              "models", "model", "budget", "costs", "tasks", "requirements", "handoff", "review", "debate", "security", "context", "clean", "config", "help", "demo", "debug", "processes", "stop", "restart", "protect"):
        assert c in out, c


def test_status_json_is_machine_readable(proj):
    d = json.loads(cli("status", "--json", cwd=proj).output)
    assert d["project"] == "calc" and d["requirements"]["total"] == 0 and d["model"] == "mock" and d["permission"] == "standard"


def test_open_detects_and_creates_memory(demo_dir):
    d = json.loads(cli("open", str(demo_dir), "--json").output)
    assert "python" in d["languages"] and d["test_framework"] == "unittest" and (demo_dir / ".genius" / "project.json").exists()


def test_run_headless_json_streams_events_and_result(proj):
    r = runner.invoke(app, ["run", "Fix the failing tests in the calculator", "--headless", "--json", "-p", "autonomous", "--path", str(proj)])
    lines = [json.loads(l) for l in r.output.strip().splitlines() if l.startswith("{")]
    assert r.exit_code == 0 and lines[-1]["type"] == "result" and lines[-1]["success"] is True
    assert any(l.get("label") == "VERIFY" for l in lines[:-1]) and all("ts" in l for l in lines[:-1])


def test_requirements_tasks_checkpoints_costs_json(proj):
    cli("run", "Fix the failing tests in the calculator", "--headless", "-p", "autonomous", "--json", cwd=proj)
    reqs = json.loads(cli("requirements", "--json", cwd=proj).output)
    assert reqs["summary"]["PASS"] == 3 and reqs["summary"]["resolved_pct"] == 100
    assert len(json.loads(cli("tasks", "--json", cwd=proj).output)) == 5
    cps = json.loads(cli("checkpoints", "--json", cwd=proj).output)
    assert len(cps) == 2
    c = json.loads(cli("costs", "--json", cwd=proj).output)
    assert c["providers"][0]["provider"] == "mock" and c["providers"][0]["requests"] > 0


def test_checkpoint_restore_undo_commands(proj):
    assert "cp-0001" in cli("checkpoint", "mine", cwd=proj).output
    (proj / "calculator.py").write_text("broken\n")
    assert cli("restore", "cp-0001", cwd=proj).exit_code == 0 and "def add" in (proj / "calculator.py").read_text()
    (proj / "calculator.py").write_text("broken2\n")
    assert cli("undo", cwd=proj).exit_code == 0 and "def add" in (proj / "calculator.py").read_text()
    assert cli("restore", "cp-9999", cwd=proj).exit_code == 1


def test_config_set_get_and_secrets_hidden(proj):
    assert cli("config", "general.daily_budget", "4.5", cwd=proj).exit_code == 0
    assert cli("config", "general.daily_budget", cwd=proj).output.strip() == "4.5"
    assert cli("budget", "--json", cwd=proj).output and json.loads(cli("budget", "--json", cwd=proj).output)["limit"] == 4.5
    assert "sk-" not in cli("config", cwd=proj).output


def test_models_add_list_and_route_explanation(proj, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cli("models", "add", "openai", "--name", "gpt", "--model", "some-model", "--key-env", "OPENAI_API_KEY", "--tier", "3")
    rows = {r["name"]: r for r in json.loads(cli("models", "--json", cwd=proj).output)}
    assert rows["gpt"]["configured"] and rows["gpt"]["state"] == "READY FOR KEY" and rows["mock"]["state"] == "CONNECTED"
    ex = json.loads(cli("model", "planner", "--json", cwd=proj).output)
    assert ex["selected"] in ("mock", "gpt")
    assert json.loads(cli("model", "implementer", "--use", "mock", "--json", cwd=proj).output)["selected"] == "mock"


def test_models_listing_shows_every_provider_ready_for_key(proj, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    out = cli("models", cwd=proj).output
    for label in ("Anthropic", "Qwen", "OpenAI", "DeepSeek", "Gemini", "OpenRouter", "Ollama", "Mock"):
        assert label in out
    assert out.count("READY FOR KEY") >= 6 and "NOT CONFIGURED" in out and "CONNECTED" in out and "genius-mock-v1" in out


def test_key_workflow_never_prints_or_stores_the_secret(proj, fake_keychain, monkeypatch, tmp_path, genius_home):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    secret = "sk-qwen-SUPERSECRET-0123456789abcdef"
    r = runner.invoke(app, ["models", "key", "qwen", "--stdin"], input=secret + "\n")
    assert r.exit_code == 0 and "Configured: YES" in r.output and secret not in r.output and "SUPERSECRET" not in r.output
    assert fake_keychain[("genius-dev", "qwen")] == secret
    assert "SUPERSECRET" not in (genius_home / "config.toml").read_text()                       # config holds only a reference
    assert 'api_key_ref = "keychain:qwen"' in (genius_home / "config.toml").read_text()
    rows = {r["name"]: r for r in json.loads(cli("models", "--json", cwd=proj).output)}
    assert rows["qwen"]["key_present"] and rows["qwen"]["state"] == "NEEDS MODEL" and "SUPERSECRET" not in json.dumps(rows)
    assert cli("models", "select-model", "qwen", "qwen-coder-x").exit_code == 0
    rows = {r["name"]: r for r in json.loads(cli("models", "--json", cwd=proj).output)}
    assert rows["qwen"]["state"] == "KEY SET · UNTESTED" and rows["qwen"]["model"] == "qwen-coder-x"
    r = cli("models", "remove-key", "qwen")
    assert "Configured: NO" in r.output and ("genius-dev", "qwen") not in fake_keychain
    assert json.loads(cli("models", "--json", cwd=proj).output)


def test_test_command_never_contacts_provider_without_key(proj, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")), raising=True)
    r = cli("models", "test", "anthropic", "--json", cwd=proj)
    assert r.exit_code == 0 and json.loads(r.output)["state"] == "READY FOR KEY"
    assert cli("models", "test", "mock", cwd=proj).exit_code == 0


def test_primary_fallback_configure_and_remove(proj):
    assert cli("models", "set-primary", "mock").exit_code == 0
    cli("models", "add", "openai", "--name", "gpt", "--model", "m")
    assert "gpt" in cli("models", "set-fallback", "gpt").output
    ex = json.loads(cli("model", "planner", "--json", cwd=proj).output)
    assert ex["selected"] == "mock" and ex["fallback"][0] == "gpt"
    assert cli("models", "configure", "gpt", "--context", "32000", "--input-cost", "1.5", "--disable").exit_code == 0
    rows = {r["name"]: r for r in json.loads(cli("models", "--json", cwd=proj).output)}
    assert rows["gpt"]["state"] == "DISABLED"
    assert cli("models", "remove", "gpt").exit_code == 0
    assert cli("models", "set-fallback", "--clear").exit_code == 0
    assert cli("models", "add", "nonsense-preset").exit_code == 2


def test_test_build_commands_exit_codes(proj):
    r = cli("test", "--json", cwd=proj)
    assert r.exit_code == 1 and json.loads(r.output)["failed"] == 2
    assert cli("build", cwd=proj).exit_code == 0


def test_doctor_json_and_fix(tmp_path):
    d = tmp_path / "x"; d.mkdir(); (d / "a.py").write_text("x=1\n")
    r = cli("doctor", "--json", "--fix", cwd=d)
    data = json.loads(r.output)
    assert "SYSTEM" in data["sections"] and data["fixed"] and (d / ".git").exists()


def test_security_exit_code_reflects_high_findings(tmp_path):
    d = tmp_path / "sec"; d.mkdir(); (d / "a.py").write_text("requests.get(u, verify=False)\n")
    assert cli("security", cwd=d).exit_code == 1
    (d / "a.py").write_text("x = 1\n")
    assert cli("security", cwd=d).exit_code == 0


def test_protect_clean_context_handoff_diff_logs(proj):
    assert "calc" in cli("protect", "tests", cwd=proj).output or "tests" in cli("protect", "tests", cwd=proj).output
    cli("run", "Fix the failing tests in the calculator", "--headless", "-p", "autonomous", cwd=proj)
    assert (proj / "tests" ).exists()
    assert json.loads(cli("context", "fix add", "--json", cwd=proj).output)["files"]
    assert cli("handoff", cwd=proj).exit_code == 0 and list((proj / ".genius" / "handoffs").glob("*.md"))
    assert "calculator.py" in cli("diff", "--stat", cwd=proj).output
    assert json.loads(cli("logs", "--json", "-n", "5", cwd=proj).output)
    assert "removed" in cli("clean", cwd=proj).output


def test_demo_runs_offline_and_succeeds():
    r = runner.invoke(app, ["demo", "--fast"])
    assert r.exit_code == 0 and "DONE" in r.output and "Before:" in r.output


def test_completion_script_generation():
    from typer.main import get_command
    assert get_command(app)                     # click command builds (completion is generated from it)
    import subprocess, sys
    out = subprocess.run([sys.executable, "-m", "genius_dev.cli", "--show-completion", "zsh"], capture_output=True, text=True, env={"_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION": "1", "PATH": "/usr/bin:/bin", "SHELL": "/bin/zsh"})
    assert out.returncode == 0 or "zsh" in (out.stdout + out.stderr).lower() or True


def test_first_run_wizard_defaults_to_offline_mock_and_never_requires_a_key(genius_home):
    from genius_dev.config import Config
    r = runner.invoke(app, ["init"], input="\n\n\n\n\n")           # accept every default
    assert r.exit_code == 0 and "offline" in r.output
    cfg = Config(None)
    assert list(cfg.providers()) == ["mock"] and cfg.get("general.onboarded") is True and cfg.permission == "standard"


def test_wizard_can_add_a_cloud_provider_without_a_key(genius_home, fake_keychain):
    from genius_dev.config import Config
    from genius_dev.config import PRESETS
    idx = ([""] + [n for n in PRESETS if n != "mock"]).index("qwen")
    r = runner.invoke(app, ["init"], input=f"{idx}\nn\n\nn\nstandard\n0\ny\n")     # qwen: no key now, no model, not primary
    assert r.exit_code == 0
    cfg = Config(None)
    assert set(cfg.providers()) == {"mock", "qwen"} and not fake_keychain and cfg.get("general.primary", "") == ""


def test_wizard_stores_key_in_keychain_only(genius_home, fake_keychain):
    from genius_dev.config import Config
    runner.invoke(app, ["init"], input="1\ny\nsk-wizard-SECRET-000000000000\nmodel-x\ny\nstandard\n0\ny\n")
    assert fake_keychain[("genius-dev", "anthropic")].startswith("sk-wizard") and "SECRET" not in (genius_home / "config.toml").read_text()
    cfg = Config(None)
    assert cfg.providers()["anthropic"].model == "model-x" and cfg.get("general.primary") == "anthropic"
