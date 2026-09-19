"""Deterministic offline model. Speaks the same JSON agent protocol as real models.

It is a *scripted* stand-in, not an intelligence: it can drive the bundled demo project
end-to-end, follow an explicit ``script`` (used by tests), and otherwise declines to edit code.
"""
from __future__ import annotations

import abc
import json
import re
import time
from typing import Any

from .base import Completion, ModelProvider, Usage, estimate_tokens

DEMO_PATCH_ADD = {"path": "calculator.py", "search": "return a - b", "replace": "return a + b"}
DEMO_PATCH_DIV = {
    "path": "calculator.py",
    "search": "def divide(a, b):\n    return a / b",
    "replace": "def divide(a, b):\n    if b == 0:\n        raise ValueError(\"division by zero\")\n    return a / b",
}


def _role(system: str) -> str:
    m = re.search(r"\[\[role:([a-z_]+)\]\]", system or "")
    return m.group(1) if m else "general"


class MockProvider(ModelProvider):
    def __init__(self, cfg, transport=None):
        super().__init__(cfg, transport)
        self.script: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None     # tests can inject failures
        self.delay = 0.0

    async def generate(self, messages, system="", tools=None, json_mode=False, max_tokens=4096, temperature=None) -> Completion:
        t = time.perf_counter()
        if self.fail_with is not None:
            raise self.fail_with
        role = _role(system)
        self.calls.append({"role": role, "messages": messages, "system": system})
        if self.delay:
            import asyncio
            await asyncio.sleep(self.delay)
        text = self.script.pop(0) if self.script else self._brain(role, system, messages)
        usage = Usage(estimate_tokens(system + "".join(m["content"] for m in messages)), estimate_tokens(text))
        return Completion(text, usage, [], self.cfg.model or "genius-mock-v1", time.perf_counter() - t, "stop")

    # -- scripted behaviour ---------------------------------------------------
    def _brain(self, role: str, system: str, messages: list[dict[str, Any]]) -> str:
        first = messages[0]["content"] if messages else ""
        last = messages[-1]["content"] if messages else ""
        sc = scenario_for(first)
        if role == "planner":
            return json.dumps(sc.plan(first) if sc else _generic_plan(first))
        if role in ("implementer", "debugger"):
            return json.dumps(sc.act(role, messages) if sc else _generic_act(messages))
        if role == "summarizer":
            return "Session summary: " + re.sub(r"\s+", " ", last)[:400]
        if role == "reviewer" and "PLAN checks" in last:
            return json.dumps({"checks": [{"hypothesis": 0, "tool": "tests", "args": {}, "why": "run the suite to see the real failures"}]})
        if role == "reviewer" and "CONCLUDE" in last:
            m = re.search(r"CHECK results?.*?:\s*(?:\n)?(.*)", last.split("CHECK RESULTS:")[-1], re.S)
            ev = re.sub(r"\s+", " ", (m.group(1) if m else "no check output"))[:160]
            failed = "FAILED" in last or "failed" in last
            return json.dumps({"verdicts": [{"hypothesis": 0, "verdict": "supported" if failed else "refuted", "evidence": ev}],
                               "conclusion": "Tests are failing; see evidence." if failed else "Tests currently pass — the hypothesis is not supported.",
                               "next_step": "Read the failing test and the code under test." if failed else "No action needed."})
        if role in ("reviewer", "final_reviewer", "security_reviewer"):
            return json.dumps({"verdict": "pass", "findings": [], "summary": "No blocking issues found in the reviewed diff (mock reviewer)."})
        if role == "ui_reviewer":
            return json.dumps({"verdict": "pass", "issues": [], "summary": "Mock reviewer: no visual model configured."})
        if role == "debater":
            hyps = ["the failure originates in the most recently changed module", "an edge case (empty / boundary input) is not handled in the code under test",
                    "state or configuration differs between the test environment and runtime"]
            return f"Hypothesis ({self.cfg.name}): {hyps[sum(map(ord, self.cfg.name)) % 3]}; verify against test output."
        if role == "commit":
            return "Update project via Genius Dev"
        return "pong" if "pong" in last else "OK"


def _goal(first: str) -> str:
    return re.sub(r"\s+", " ", first.split("GOAL:")[-1]).strip()[:140]


def _generic_plan(first: str) -> dict[str, Any]:
    goal = _goal(first)
    return {"requirements": [{"description": goal or "Complete the requested work", "verify": "tests", "files": []}], "tasks": [{"title": goal or "Complete the requested work", "reqs": [0]}]}


def _generic_act(messages: list[dict[str, Any]]) -> dict[str, Any]:
    if _turn(messages) == 0:
        return {"summary": "Checking current test status", "actions": [{"tool": "tests", "args": {}}], "done": False}
    return {"summary": "Mock model cannot author code changes for this project", "actions": [], "done": True,
            "final": "The offline mock model only knows its bundled demo scenarios and made no edits. Configure a real provider to build features."}


def _turn(messages: list[dict[str, Any]]) -> int:
    return sum(1 for m in messages if m["role"] == "assistant")


def _step(summary, *actions, done=False, final=""):
    return {"summary": summary, "actions": [{"tool": t, "args": a} for t, a in actions], "done": done, "final": final}


# ------------------------------------------------------------------------------------------------ scenarios
class Scenario(abc.ABC):
    name = "scenario"

    @abc.abstractmethod
    def matches(self, first: str) -> bool: ...

    @abc.abstractmethod
    def plan(self, first: str) -> dict[str, Any]: ...

    @abc.abstractmethod
    def act(self, role: str, messages: list[dict[str, Any]]) -> dict[str, Any]: ...


class CalculatorFix(Scenario):
    """Two independent bugs; one clean repair pass. `genius demo` (default)."""
    name = "fix"

    def matches(self, first):
        return "calculator.py" in first

    def plan(self, first):
        u = "cmd:{py} -m unittest -q tests.test_calculator.TestCalculator."
        return {"requirements": [
            {"description": "add(a, b) returns the sum of its arguments", "verify": u + "test_add", "files": ["calculator.py"]},
            {"description": "divide(a, 0) raises ValueError instead of ZeroDivisionError", "verify": u + "test_divide_by_zero", "files": ["calculator.py"]},
            {"description": "Full test suite passes", "verify": "tests", "files": []}],
            "tasks": [{"title": "Reproduce failing tests", "reqs": [0, 1]}, {"title": "Fix calculator.py", "reqs": [0, 1], "children": ["Fix add()", "Guard divide()"]},
                      {"title": "Verify full suite", "reqs": [2]}]}

    def act(self, role, messages):
        steps = [_step("Running the test suite to reproduce the failures", ("tests", {})), _step("Reading calculator.py", ("read_file", {"path": "calculator.py"})),
                 _step("add() subtracts — fixing", ("patch_file", DEMO_PATCH_ADD)), _step("divide() has no zero guard — fixing", ("patch_file", DEMO_PATCH_DIV)),
                 _step("Re-running tests", ("tests", {})),
                 _step("Fixed add() and divide(); suite is green", done=True, final="Fixed add() (was subtracting) and divide() (now raises ValueError on zero).")]
        return steps[min(_turn(messages), len(steps) - 1)]


SLUG_GOOD = 'import re\n\n\ndef slugify(text):\n    text = re.sub(r"[^a-z0-9\\s-]", "", text.lower())\n    return re.sub(r"[\\s-]+", "-", text).strip("-")\n'


class SlugDebug(Scenario):
    """A too-shallow first fix fails verification; the debugger diagnoses from the failing output and repairs it."""
    name = "debug"

    def matches(self, first):
        return "slug.py" in first

    def plan(self, first):
        t = "cmd:{py} -m unittest -q tests.test_slug.TestSlug."
        return {"requirements": [
            {"description": "slugify lowercases and joins words with hyphens", "verify": t + "test_basic", "files": ["slug.py"]},
            {"description": "slugify removes punctuation", "verify": t + "test_punctuation", "files": ["slug.py"]},
            {"description": "slugify collapses repeated whitespace into one hyphen", "verify": t + "test_collapse", "files": ["slug.py"]},
            {"description": "slugify trims leading and trailing whitespace", "verify": t + "test_strip", "files": ["slug.py"]},
            {"description": "Full test suite passes", "verify": "tests", "files": []}],
            "tasks": [{"title": "Reproduce failures", "reqs": [1, 2, 3]}, {"title": "Fix slugify()", "reqs": [0, 1, 2, 3]}, {"title": "Verify full suite", "reqs": [4]}]}

    def act(self, role, messages):
        turn = _turn(messages)
        if role == "implementer":
            quick = {"path": "slug.py", "search": 'return text.lower().replace(" ", "-")', "replace": 'return text.strip().lower().replace(" ", "-")'}
            steps = [_step("Running the tests", ("tests", {})), _step("Reading slug.py", ("read_file", {"path": "slug.py"})),
                     _step("Quick fix: trim whitespace before joining", ("patch_file", quick)), _step("Re-running the tests", ("tests", {})),
                     _step("Whitespace trimming added", done=True, final="Trimmed leading/trailing whitespace.")]
            return steps[min(turn, len(steps) - 1)]
        first = messages[0]["content"]
        why = [n for n in ("test_punctuation", "test_collapse") if n in first]
        steps = [_step(f"Diagnosis from the failing output: {' and '.join(why) or 'remaining failures'} — the quick fix only handled whitespace at the ends", ("read_file", {"path": "tests/test_slug.py"})),
                 _step("Rewriting slugify(): strip punctuation, collapse whitespace/hyphens, trim hyphens", ("write_file", {"path": "slug.py", "content": SLUG_GOOD})),
                 _step("Re-running the tests", ("tests", {})),
                 _step("All slug behaviours verified", done=True, final="Rewrote slugify() to remove punctuation and collapse separators (the earlier trim-only patch was insufficient).")]
        return steps[min(turn, len(steps) - 1)]


PAGE_BAD = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Acme</title></head>\n'
    '<body style="margin:0;font-family:Arial,sans-serif">\n<h1 style="color:#cccccc;padding:24px">Acme makes shipping easy</h1>\n'
    '<div style="width:960px;background:#eeeeee;padding:24px">Hero banner (fixed width)</div>\n'
    '<a href="/start" style="padding:24px"><button></button></a>\n</body></html>\n')
PAGE_GOOD = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Acme — shipping made easy</title>\n'
    '<style>\n:root{--ink:#111827;--muted:#4b5563;--accent:#1d4ed8;--bg:#ffffff;--soft:#f3f4f6}\n'
    '*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:var(--ink);background:var(--bg);line-height:1.5}\n'
    'header,main,footer{max-width:960px;margin:0 auto;padding:24px}\nh1{font-size:clamp(28px,5vw,44px);line-height:1.15;margin:0 0 12px}p{color:var(--muted);max-width:60ch;margin:0 0 24px}\n'
    '.hero{background:var(--soft);border-radius:12px;padding:32px;max-width:100%}\n'
    '.cta{display:inline-block;min-height:44px;padding:12px 20px;background:var(--accent);color:#fff;border:0;border-radius:8px;font-size:16px;text-decoration:none}\n'
    'nav a{color:var(--ink);padding:12px 8px;display:inline-block;min-height:44px}\n</style></head>\n<body>\n'
    '<header><nav aria-label="Main"><a href="/">Acme</a> <a href="/pricing.html">Pricing</a></nav></header>\n'
    '<main><section class="hero"><h1>Acme makes shipping easy</h1><p>Book, track and deliver parcels from one dashboard — no spreadsheets.</p>\n'
    '<a class="cta" href="/start.html">Get started</a></section></main>\n<footer><p>&copy; Acme</p></footer>\n</body></html>\n')


class WebLanding(Scenario):
    """Builds a landing page; the first draft has real layout/a11y failures that the browser audit catches; the debugger repairs them."""
    name = "web"

    def matches(self, first):
        return "landing page" in first.split("GOAL:")[-1].lower() or "acme marketing site" in first.lower()

    def plan(self, first):
        chk = "cmd:{py} -c \"import pathlib,sys; t=pathlib.Path('index.html').read_text(); sys.exit(0 if 'Get started' in t and '<h1' in t else 1)\""
        return {"requirements": [
            {"description": "index.html exists with a heading and a 'Get started' call to action", "verify": chk, "files": ["index.html"]},
            {"description": "The page passes the browser audit on desktop and mobile (no overflow, labelled controls, no console errors)", "verify": "browser", "files": ["index.html"]}],
            "tasks": [{"title": "Draft the landing page", "reqs": [0]}, {"title": "Pass visual QA", "reqs": [1]}]}

    def act(self, role, messages):
        turn = _turn(messages)
        if role == "implementer":
            steps = [_step("Drafting index.html", ("write_file", {"path": "index.html", "content": PAGE_BAD})),
                     _step("Draft written; visual QA will audit it", done=True, final="Created index.html.")]
            return steps[min(turn, len(steps) - 1)]
        first = messages[0]["content"]
        found = [k for k in ("horizontal scroll", "no accessible name") if k in first]
        steps = [_step(f"Visual QA failed ({', '.join(found) or 'see report'}): the fixed-width banner overflows on mobile and the button has no accessible name", ("read_file", {"path": "index.html"})),
                 _step("Rewriting the page with a fluid layout, a labelled call-to-action and readable contrast", ("write_file", {"path": "index.html", "content": PAGE_GOOD})),
                 _step("Layout repaired", done=True, final="Made the layout fluid, labelled the CTA, and raised contrast.")]
        return steps[min(turn, len(steps) - 1)]


SCENARIOS: list[Scenario] = [SlugDebug(), WebLanding(), CalculatorFix()]


def scenario_for(first: str) -> Scenario | None:
    return next((s for s in SCENARIOS if s.matches(first)), None)
