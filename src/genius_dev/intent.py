"""Interpret natural-language input: safe human-override commands vs. a work request for the agent."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Intent:
    kind: str                   # stop|pause|resume_run|model|protect|restore|skip|diff|view|command|resume|finish|plan|goal|empty
    args: dict[str, Any] = field(default_factory=dict)


ROLE_WORDS = {"coding": "implementer", "code": "implementer", "implementation": "implementer", "planning": "planner", "plan": "planner",
              "debugging": "debugger", "review": "reviewer", "reviews": "reviewer", "tests": "tester", "testing": "tester",
              "ui": "ui_reviewer", "security": "security_reviewer", "docs": "docs", "documentation": "docs"}
VIEWS = {"tasks", "requirements", "models", "tools", "tests", "preview", "diff", "logs", "checkpoints", "cost", "costs", "project", "settings", "home"}


def interpret(text: str, agent_active: bool = False) -> Intent:
    t = text.strip()
    low = t.lower().rstrip(".!")
    if not t:
        return Intent("empty")
    if low in ("stop", "halt", "cancel", "abort", "stop!"):
        return Intent("stop")
    if low == "pause":
        return Intent("pause")
    if low in ("resume", "continue", "go on", "unpause") and agent_active:
        return Intent("resume_run")
    if m := re.match(r"(?:change|switch|set)\s+(?:the\s+)?model\s+to\s+(.+)", low):
        return Intent("model", {"provider": m.group(1).strip(), "role": None})
    if m := re.match(r"use\s+(.+?)\s+for\s+(.+)", low):
        role = ROLE_WORDS.get(m.group(2).strip().split()[0], m.group(2).strip())
        return Intent("model", {"provider": m.group(1).strip(), "role": role})
    if m := re.match(r"(?:don'?t|do not|never)\s+(?:touch|modify|edit|change)\s+(?:the\s+)?(.+)", low):
        return Intent("protect", {"target": m.group(1).strip().strip("/ ")})
    if m := re.match(r"(?:remember(?: that)?|from now on|always|prefer)[:,]?\s+(.+)", t, re.I):
        return Intent("remember", {"text": (t if t.lower().startswith(("always", "prefer")) else m.group(1)).strip()})
    if re.match(r"(restore|roll ?back|revert)\s+(to\s+)?(the\s+)?(last|latest|previous)\s+checkpoint|^undo$", low):
        return Intent("restore")
    if m := re.match(r"restore\s+(cp-\d+)", low):
        return Intent("restore", {"id": m.group(1)})
    if m := re.match(r"skip\s+(?:this\s+)?(?:requirement\s*)?(req-\d+)?", low):
        return Intent("skip", {"id": m.group(1).upper() if m.group(1) else None})
    if re.match(r"(show|see)\s+(me\s+)?(what\s+)?(changed|the diff|diff)", low) or low in ("diff", "what changed"):
        return Intent("diff")
    if low in VIEWS:
        return Intent("view", {"view": low})
    vm = re.match(r"(?:show|open|go to)\s+(?:me\s+)?(?:the\s+)?(\w+)$", low)
    if vm and vm.group(1) in VIEWS:
        return Intent("view", {"view": vm.group(1)})
    if re.match(r"(continue|resume|pick up)\b.*(where|left off|stopped|yesterday|last)", low) or low in ("resume", "continue"):
        return Intent("resume")
    if m := re.match(r"budget\s+\$?([\d.]+)", low):
        return Intent("command", {"cmd": "budget", "value": float(m.group(1))})
    if m := re.match(r"(?:mode|routing)\s+(economy|balanced|max[_ ]quality|local[_ ]only|custom)", low):
        return Intent("command", {"cmd": "mode", "value": m.group(1).replace(" ", "_")})
    if m := re.match(r"permissions?\s+(safe|standard|autonomous)", low):
        return Intent("command", {"cmd": "permission", "value": m.group(1)})
    if low in ("doctor", "status", "help", "handoff", "review", "security", "context", "checkpoint", "finish", "debug", "clean", "preview", "processes"):
        return Intent("command", {"cmd": low})
    if re.search(r"production[- ]ready|everything preventing|finish (the )?(project|app)|is (it|this) done", low):
        return Intent("finish")
    if m := re.match(r"plan\b[:\s]+(.+)", t, re.I):
        return Intent("plan", {"goal": m.group(1)})
    if m := re.match(r"debate\b[:\s]+(.+)", t, re.I):
        return Intent("command", {"cmd": "debate", "text": m.group(1)})
    return Intent("goal", {"goal": t})
