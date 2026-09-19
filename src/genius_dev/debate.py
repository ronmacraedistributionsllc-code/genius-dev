"""genius debate — independent hypotheses from several models, judged against real evidence."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from .agent import extract_json
from .context import ContextBuilder
from .permissions import Permissions
from .project import Project
from .events import EventBus
from .router import Router
from .runner import truncate
from .tools import ToolBox

READ_ONLY = {"read_file", "search_files", "search_symbols", "tests", "logs", "list_directory", "git", "project_index", "http", "docs_lookup"}

JUDGE_SYSTEM = """[[role:reviewer]]
You are a debugging judge. You receive an issue, evidence gathered from the real project, and hypotheses from several engineers.
Step 1 (when asked to PLAN): reply JSON {"checks":[{"hypothesis":0,"tool":"<read-only tool>","args":{...},"why":""}]} with at most 5 checks that would
confirm or refute the hypotheses. Allowed tools: read_file, search_files, search_symbols, tests, logs, git (status/diff/log), project_index.
Step 2 (when asked to CONCLUDE): reply JSON {"verdicts":[{"hypothesis":0,"verdict":"supported|refuted|unproven","evidence":""}],"conclusion":"","next_step":""}.
Base verdicts ONLY on the tool output shown, not on plausibility."""


@dataclass
class DebateResult:
    issue: str
    hypotheses: list[dict[str, str]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    conclusion: str = ""
    next_step: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    def text(self) -> str:
        lines = [f"DEBATE — {self.issue}", ""]
        for i, h in enumerate(self.hypotheses):
            v = next((x for x in self.verdicts if x.get("hypothesis") == i), {})
            lines.append(f"  {h['provider']:12} {h['claim'][:110]}")
            if v:
                lines.append(f"  {'':12} → {v.get('verdict', '?').upper()}: {str(v.get('evidence', ''))[:140]}")
        lines += ["", "EVIDENCE CHECKS"] + [f"  {c['tool']} {json.dumps(c['args'])[:70]} → {c['result'][:80]}" for c in self.checks]
        lines += ["", "CONCLUSION", f"  {self.conclusion}", "", "NEXT", f"  {self.next_step}"]
        return "\n".join(lines)


async def run_debate(p: Project, router: Router, perms: Permissions, bus: EventBus, issue: str, max_models: int = 3) -> DebateResult:
    res = DebateResult(issue)
    tools = ToolBox(p, perms, bus)
    await asyncio.to_thread(p.index.refresh)
    failing = ""
    if p.info.test_cmd:
        t = await tools.execute("tests", {})
        failing = "" if t.ok else t.output
    pack = ContextBuilder(p, 12_000).build(issue, failing)
    evidence = pack.text
    provs = [c for c in router.configs().values() if c.enabled and c.name not in router.auth_failed]
    if router.mode == "local_only" or not p.cfg.get("privacy.cloud_allowed", True):
        provs = [c for c in provs if c.local]
    provs = sorted(provs, key=lambda c: -c.tier)[:max_models]
    if not provs:
        res.conclusion = "No provider available for a debate."
        return res
    bus.emit("AGENT", "DEBATE", f"asking {', '.join(c.name for c in provs)} independently")

    async def ask(name: str) -> dict[str, str]:
        router.pins["debater"] = name
        # Independent asks: each pinned provider answers alone (no shared transcript).
        prov = router.provider(name)
        try:
            comp = await prov.generate([{"role": "user", "content": f"{evidence}\n\nISSUE: {issue}\nGive ONE root-cause hypothesis in <=2 sentences, and name the file most likely responsible."}],
                                       "[[role:debater]]\nYou are a senior engineer diagnosing a bug. Be specific and falsifiable.", max_tokens=400)
            router._record(prov, "debater", comp, __import__("time").time() - comp.latency)
            return {"provider": name, "claim": comp.text.strip().replace("\n", " ")}
        except Exception as e:  # noqa: BLE001
            return {"provider": name, "claim": f"(no answer: {str(e)[:60]})"}
    res.hypotheses = list(await asyncio.gather(*[ask(c.name) for c in provs]))
    router.pins.pop("debater", None)
    hyp_text = "\n".join(f"[{i}] {h['provider']}: {h['claim']}" for i, h in enumerate(res.hypotheses))
    plan = await router.complete("reviewer", [{"role": "user", "content": f"PLAN checks.\nISSUE: {issue}\n\nEVIDENCE:\n{truncate(evidence, 9000)}\n\nHYPOTHESES:\n{hyp_text}"}], JUDGE_SYSTEM, json_mode=True)
    checks = (extract_json(plan.text) or {}).get("checks", [])[:5]
    outputs: list[str] = []
    for c in checks:
        name, args = c.get("tool", ""), c.get("args") or {}
        if name not in READ_ONLY:
            continue
        r = await tools.execute(name, args)
        res.checks.append({"tool": name, "args": args, "result": r.summary, "hypothesis": c.get("hypothesis")})
        outputs.append(f"CHECK {name} {json.dumps(args)[:120]} (for hypothesis {c.get('hypothesis')}):\n{r.for_model(1500)}")
    final = await router.complete("reviewer", [{"role": "user", "content": f"CONCLUDE.\nISSUE: {issue}\n\nHYPOTHESES:\n{hyp_text}\n\nCHECK RESULTS:\n" + ("\n\n".join(outputs) or "(no checks could be run)")}], JUDGE_SYSTEM, json_mode=True)
    data = extract_json(final.text) or {}
    res.verdicts = data.get("verdicts", [])
    res.conclusion = data.get("conclusion") or "The judge returned no conclusion."
    res.next_step = data.get("next_step", "")
    p.log_decision(f"debate: {issue[:80]}", res.conclusion[:200], "debate")
    return res
