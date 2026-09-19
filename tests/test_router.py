import pytest

from genius_dev.providers import ProviderError
from genius_dev.router import AllProvidersFailed, BudgetExceeded

P = {"cheap": {"tier": 1, "input_cost": 0.1, "output_cost": 0.2, "local": False}, "mid": {"tier": 2, "input_cost": 1, "output_cost": 2, "local": False},
     "strong": {"tier": 3, "input_cost": 5, "output_cost": 25, "local": False}, "local": {"tier": 1, "local": True}}


@pytest.fixture
def rt(tmp_path, make_rt):
    (tmp_path / "p").mkdir()
    return make_rt(tmp_path / "p", P)


def test_balanced_routes_planning_strong_and_coding_cheap(rt):
    assert rt.router.route("planner").primary == "strong"
    assert rt.router.route("implementer").primary in ("cheap", "local")
    assert rt.router.route("security_reviewer").primary == "strong"
    assert rt.router.route("summarizer").primary in ("cheap", "local")


def test_max_quality_and_economy_and_local_only(rt):
    rt.router.mode_override = "max_quality"
    assert rt.router.route("implementer").primary == "strong"
    rt.router.mode_override = "economy"
    assert rt.router.route("debugger").primary in ("cheap", "local")
    rt.router.mode_override = "local_only"
    r = rt.router.route("planner")
    assert r.chain == ["local"] and "local-only" in r.reason


def test_custom_routing_and_pin_override_and_explanation(rt):
    rt.project.cfg.set("routing.custom.implementer", "mid", "project")
    assert rt.router.route("implementer").primary == "mid"
    rt.router.pin("implementer", "strong")
    r = rt.router.route("implementer")
    assert r.primary == "strong" and "pinned" in r.reason
    ex = r.explain()
    assert ex["task"] == "coding" and ex["selected"] == "strong" and isinstance(ex["fallback"], list) and ex["reason"]
    with pytest.raises(KeyError):
        rt.router.pin("planner", "nonexistent")


def test_configured_fallback_chain_order(rt):
    rt.project.cfg.set("fallback.chain", ["mid", "strong"], "project")
    rt.router.mode_override = "economy"
    r = rt.router.route("implementer")
    assert r.chain[1:3] == ["mid", "strong"]


def test_vision_role_requires_image_capable_provider(tmp_path, make_rt):
    (tmp_path / "v").mkdir()
    rt = make_rt(tmp_path / "v", {"text": {"tier": 3, "local": False}, "vis": {"tier": 2, "capabilities": ["images"], "local": False}})
    assert rt.router.route("ui_reviewer").chain == ["vis"]


def test_privacy_cloud_disabled_forces_local(rt):
    rt.project.cfg.set("privacy.cloud_allowed", False, "project")
    assert rt.router.route("planner").chain == ["local"]


async def test_fallback_on_outage_then_success(rt):
    rt.router.mode_override = "max_quality"
    rt.router.provider("strong").fail_with = ProviderError("outage", "server down")
    c = await rt.router.complete("planner", [{"role": "user", "content": "GOAL: x"}], "[[role:planner]]")
    assert c.text
    assert "strong" in rt.router.cooldown                 # cooled down, not hammered
    assert any(e.label == "FALLBACK" for e in rt.bus.buffer)


async def test_auth_failure_is_not_retried_or_reused(rt):
    rt.router.mode_override = "max_quality"
    strong = rt.router.provider("strong")
    strong.fail_with = ProviderError("auth", "bad key")
    await rt.router.complete("planner", [{"role": "user", "content": "x"}], "[[role:planner]]")
    assert len(strong.calls) == 0 and "strong" in rt.router.auth_failed        # 1 attempt only
    assert "strong" not in rt.router.route("planner").chain                     # excluded from future routes


async def test_rate_limit_retries_once_then_falls_back(rt):
    rt.router.mode_override = "max_quality"
    rt.router.max_wait = 0.01
    strong = rt.router.provider("strong")
    n = {"c": 0}
    async def flaky(*a, **k):
        n["c"] += 1
        raise ProviderError("rate_limit", "429", retry_after=0.01)
    strong.generate = flaky
    c = await rt.router.complete("planner", [{"role": "user", "content": "x"}], "[[role:planner]]")
    assert n["c"] == 2 and c.text          # one retry, no infinite loop


async def test_all_providers_failing_raises_with_attempts(rt):
    for n in P:
        rt.router.provider(n).fail_with = ProviderError("outage", "down")
    with pytest.raises(AllProvidersFailed) as e:
        await rt.router.complete("planner", [{"role": "user", "content": "x"}], "[[role:planner]]")
    assert len(e.value.attempts) == 4


async def test_no_providers_configured_reports_clearly(tmp_path, make_rt):
    (tmp_path / "n").mkdir()
    rt = make_rt(tmp_path / "n", {})
    with pytest.raises(AllProvidersFailed) as e:
        await rt.router.complete("planner", [{"role": "user", "content": "x"}])
    assert "no usable provider" in str(e.value)


async def test_calls_are_recorded_with_cost(rt):
    rt.router.mode_override = "max_quality"
    await rt.router.complete("planner", [{"role": "user", "content": "x" * 4000}], "[[role:planner]]")
    row = rt.gstore.usage_by_provider()[0]
    assert row["provider"] == "strong" and row["requests"] == 1 and row["cost"] > 0 and row["input_tokens"] > 0


async def test_secrets_are_redacted_before_cloud_but_not_local(rt):
    rt.router.mode_override = "max_quality"
    key = "sk-ant-abcdefghijklmnopqrstuvwxyz0123"
    await rt.router.complete("planner", [{"role": "user", "content": f"key is {key}"}], "[[role:planner]]")
    sent = rt.router.provider("strong").calls[0]["messages"][0]["content"]
    assert key not in sent and "REDACTED" in sent
    rt.router.mode_override = "local_only"
    await rt.router.complete("implementer", [{"role": "user", "content": f"key is {key}"}], "[[role:implementer]]")
    assert key in rt.router.provider("local").calls[0]["messages"][0]["content"]


async def test_external_context_cap_trims_oldest(rt):
    rt.project.cfg.set("privacy.max_external_context_chars", 3000, "project")
    rt.router.mode_override = "max_quality"
    msgs = [{"role": "user", "content": "first"}] + [{"role": "user" if i % 2 else "assistant", "content": "y" * 1500} for i in range(6)]
    await rt.router.complete("planner", msgs, "[[role:planner]]")
    sent = rt.router.provider("strong").calls[0]["messages"]
    assert sent[0]["content"] == "first" and sum(len(m["content"]) for m in sent) <= 3000 + 1500


# ---- budget --------------------------------------------------------------------------------
def _spend(rt, amount):
    rt.gstore.execute("INSERT INTO model_calls(ts,project,provider,model,role,input_tokens,output_tokens,cost,latency,ok) VALUES(strftime('%s','now'),'p','strong','m','x',1,1,?,0,1)", (amount,))


def test_budget_states(rt):
    rt.project.cfg.set("general.daily_budget", 5.0, "project")
    assert rt.router.budget_state() == "ok"
    _spend(rt, 4.2)
    assert rt.router.budget_state() == "near"
    _spend(rt, 1.0)
    assert rt.router.budget_state() == "over"


def test_near_budget_switches_to_local_or_cheaper(rt):
    rt.project.cfg.set("general.daily_budget", 5.0, "project")
    _spend(rt, 4.5)
    r = rt.router.route("planner")
    assert r.primary == "local" and "budget near" in r.reason


async def test_over_budget_blocks_cloud_but_in_flight_work_gets_grace(tmp_path, make_rt):
    (tmp_path / "b").mkdir()
    rt = make_rt(tmp_path / "b", {"strong": {"tier": 3, "input_cost": 5, "local": False}})
    rt.project.cfg.set("general.daily_budget", 1.0, "project")
    _spend(rt, 1.05)                                          # 105%: over, within the 10% grace
    assert rt.router.route("implementer", critical=True).primary == "strong"
    assert "in flight" in rt.router.route("implementer", critical=True).reason
    assert rt.router.route("planner").chain == []             # new non-critical work is refused for cloud models
    with pytest.raises(BudgetExceeded):
        await rt.router.complete("planner", [{"role": "user", "content": "x"}])
    _spend(rt, 1.0)                                           # 205%: even in-flight work stops
    assert rt.router.route("implementer", critical=True).chain == []


async def test_over_budget_still_serves_local_models(rt):
    rt.project.cfg.set("general.daily_budget", 1.0, "project")
    _spend(rt, 5.0)
    c = await rt.router.complete("planner", [{"role": "user", "content": "x"}], "[[role:planner]]")
    assert c.text and rt.router.provider("local").calls and not rt.router.provider("strong").calls


async def test_redaction_is_announced(rt):
    rt.router.mode_override = "max_quality"
    await rt.router.complete("planner", [{"role": "user", "content": "AKIAABCDEFGHIJKLMNOP"}], "[[role:planner]]")
    assert any(e.label == "REDACT" and "aws-access-key" in e.message for e in rt.bus.buffer)
