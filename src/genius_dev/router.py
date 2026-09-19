"""Model routing, fallback chains, budget enforcement and cost accounting."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from .config import Config, ProviderConfig
from .events import EventBus
from .providers import Completion, ModelProvider, ProviderError, build_provider, estimate_tokens
from .secrets import find_secrets, redact

# role -> (category, needs_strength)   strength: 3 strongest .. 1 economical
ROLE_PROFILE: dict[str, tuple[str, int]] = {
    "planner": ("planning", 3), "architect": ("architecture", 3), "implementer": ("coding", 1),
    "debugger": ("debugging", 3), "tester": ("test generation", 1), "ui_reviewer": ("UI critique", 3),
    "security_reviewer": ("security review", 3), "final_reviewer": ("final review", 3), "reviewer": ("code review", 3),
    "summarizer": ("documentation/summary", 1), "docs": ("documentation", 1), "debater": ("debate", 2),
    "commit": ("commit message", 1), "general": ("general", 2),
}
MODES = ["economy", "balanced", "max_quality", "local_only", "custom"]


class BudgetExceeded(Exception):
    pass


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[tuple[str, str]]):
        self.attempts = attempts
        super().__init__("; ".join(f"{p}: {e}" for p, e in attempts) or "no provider available")


@dataclass
class Route:
    role: str
    chain: list[str]
    reason: str
    budget_state: str = "ok"     # ok | near | over

    @property
    def primary(self) -> str | None:
        return self.chain[0] if self.chain else None

    def explain(self) -> dict[str, Any]:
        return {"task": ROLE_PROFILE.get(self.role, ("general", 2))[0], "selected": self.primary,
                "fallback": self.chain[1:], "reason": self.reason, "budget": self.budget_state}


class Router:
    def __init__(self, cfg: Config, project_store, global_store, bus: EventBus, project_name: str = "",
                 transport=None, session_id: int | None = None):
        self.cfg, self.pstore, self.gstore, self.bus = cfg, project_store, global_store, bus
        self.project_name, self.transport, self.session_id = project_name, transport, session_id
        self._instances: dict[str, ModelProvider] = {}
        self.pins: dict[str, str] = {}              # role/category -> provider (human override)
        self.mode_override: str | None = None
        self.auth_failed: set[str] = set()
        self.cooldown: dict[str, float] = {}         # provider -> monotonic time until usable
        self.last_route: Route | None = None
        self.last_used: dict[str, float] = {}
        self.max_wait = 5.0

    # -- providers ------------------------------------------------------------
    def configs(self) -> dict[str, ProviderConfig]:
        return self.cfg.providers()

    def provider(self, name: str) -> ModelProvider:
        if name not in self._instances:
            self._instances[name] = build_provider(self.configs()[name], self.transport)
        return self._instances[name]

    def refresh(self) -> None:
        self._instances.clear()

    @property
    def mode(self) -> str:
        return self.mode_override or self.cfg.mode

    # -- budget ---------------------------------------------------------------
    def budget(self) -> tuple[float, float]:
        limit = float(self.cfg.get("general.daily_budget", 0) or 0)
        return self.gstore.cost_today(), limit

    def budget_state(self) -> str:
        spent, limit = self.budget()
        if limit <= 0:
            return "ok"
        return "over" if spent >= limit else "near" if spent >= 0.8 * limit else "ok"

    # -- routing --------------------------------------------------------------
    def _usable(self, needs_vision=False, min_context=0) -> list[ProviderConfig]:
        now = time.monotonic()
        out = []
        for c in self.configs().values():
            if not c.enabled or c.name in self.auth_failed or self.cooldown.get(c.name, 0) > now:
                continue
            if needs_vision and "images" not in c.capabilities:
                continue
            if min_context and c.context_window < min_context:
                continue
            out.append(c)
        return out

    @staticmethod
    def _price(c: ProviderConfig) -> float:
        return c.input_cost + c.output_cost

    def route(self, role: str, needs_vision: bool = False, min_context: int = 0, critical: bool = False) -> Route:
        category, strength = ROLE_PROFILE.get(role, ROLE_PROFILE["general"])
        cands = self._usable(needs_vision or role == "ui_reviewer", min_context)
        mode, state = self.mode, self.budget_state()
        reasons: list[str] = []
        if not self.cfg.get("privacy.cloud_allowed", True):
            cands = [c for c in cands if c.local]
            reasons.append("cloud disabled by privacy settings")
        if mode == "local_only":
            cands = [c for c in cands if c.local]
            reasons.append("local-only mode")
        spent, limit = self.budget()
        if state == "over" and (not critical or spent >= 1.10 * limit):
            # hard stop: cloud spend is over budget (critical/in-flight work gets a 10% grace for estimate drift)
            cands = [c for c in cands if c.local]
            reasons.append("daily budget reached: local models only")
        elif state != "ok" and not critical:
            local = [c for c in cands if c.local]
            cheap = sorted(cands, key=self._price)
            cands = local or cheap[: max(1, len(cheap) // 2)]
            reasons.append(f"budget {state}: restricted to cheaper/local models")
        elif state == "over" and critical:
            reasons.append("budget over but operation is in flight — allowed to finish (10% grace)")
        if not cands:
            return Route(role, [], "no usable provider (" + (", ".join(reasons) or "none configured/enabled") + ")", state)

        pin = self.pins.get(role) or self.pins.get(category)
        custom = self.cfg.get("routing.custom", {}) or {}
        ordered: list[ProviderConfig]
        if pin and any(c.name == pin for c in cands):
            ordered = [next(c for c in cands if c.name == pin)]
            reasons.insert(0, f"pinned by user to {pin}")
        elif custom.get(role) or custom.get(category):
            want = custom.get(role) or custom.get(category)
            hit = [c for c in cands if c.name == want]
            ordered = hit or []
            reasons.insert(0, f"custom routing → {want}" + ("" if hit else " (unavailable)"))
        elif (prim := self.cfg.get("general.primary", "")) and any(c.name == prim for c in cands):
            ordered = [next(c for c in cands if c.name == prim)]
            reasons.insert(0, f"primary provider set by user ({prim})")
        else:
            ordered = []
        if not ordered:
            if mode == "max_quality":
                want_strong, why = True, "max-quality mode"
            elif mode == "economy":
                want_strong, why = (strength == 3 and role in ("planner", "architect")), "economy mode"
            else:
                want_strong, why = strength >= 3, "balanced mode"
            key = (lambda c: (-c.tier, self._price(c))) if want_strong else (lambda c: (c.tier, self._price(c)))
            ordered = sorted(cands, key=key)
            reasons.insert(0, f"{category} task, {why} → {'strongest' if want_strong else 'economical'} available model")
        # fallback chain: configured chain first, then remaining by strength
        chain_cfg = [n for n in self.cfg.get("fallback.chain", []) or [] if any(c.name == n for c in cands)]
        names = [c.name for c in ordered[:1]]
        for n in chain_cfg + [c.name for c in ordered[1:]] + [c.name for c in sorted(cands, key=lambda c: -c.tier)]:
            if n not in names:
                names.append(n)
        r = Route(role, names, "; ".join(reasons), state)
        self.last_route = r
        return r

    # -- execution with fallback ----------------------------------------------
    def _prepare(self, messages: list[dict], system: str, provider: ProviderConfig) -> tuple[list[dict], str]:
        """Apply privacy: redact secrets and cap external context."""
        if provider.local or not self.cfg.get("privacy.redact_secrets", True):
            return messages, system
        limit = int(self.cfg.get("privacy.max_external_context_chars", 120_000))
        found = sorted({k for m in messages for k in find_secrets(m["content"])})
        if found:
            self.bus.emit("MODEL", "REDACT", f"redacted {', '.join(found)} before sending to {provider.name}")
        out = [{**m, "content": redact(m["content"])} for m in messages]
        total = sum(len(m["content"]) for m in out) + len(system)
        while total > limit and len(out) > 1:
            dropped = out.pop(1)          # keep the first (task framing); drop oldest after it
            total -= len(dropped["content"])
        return out, redact(system)

    async def complete(self, role: str, messages: list[dict], system: str = "", critical: bool = False,
                       json_mode: bool = False, max_tokens: int = 4096, tools=None, images: bool = False) -> Completion:
        est = estimate_tokens(system + "".join(m["content"] for m in messages))
        route = self.route(role, needs_vision=images, min_context=int(est * 1.1), critical=critical)
        if not route.chain:
            if route.budget_state == "over":
                raise BudgetExceeded(route.reason)
            raise AllProvidersFailed([("router", route.reason)])
        self.bus.emit("MODEL", "ROUTE", f"{role} → {route.primary}", reason=route.reason, chain=route.chain)
        attempts: list[tuple[str, str]] = []
        tried: set[str] = set()
        for name in route.chain:
            if name in tried:
                continue
            tried.add(name)
            prov = self.provider(name)
            msgs, sysm = self._prepare(messages, system, prov.cfg)
            for attempt in range(2):                 # at most one retry, only for short rate limits
                t0 = time.time()
                try:
                    comp = await prov.generate(msgs, sysm, tools=tools, json_mode=json_mode, max_tokens=max_tokens)
                except ProviderError as e:
                    self._record(prov, role, None, t0, str(e))
                    attempts.append((name, f"{e.kind}: {e}"))
                    self.bus.emit("ERRORS", "MODEL", f"{name} failed ({e.kind})", detail=str(e))
                    if e.kind == "rate_limit" and attempt == 0 and (e.retry_after or 1) <= self.max_wait:
                        await asyncio.sleep(e.retry_after or 1)
                        continue
                    if e.kind == "auth":
                        self.auth_failed.add(name)       # never retry bad credentials this session
                    elif e.kind in ("rate_limit", "outage"):
                        self.cooldown[name] = time.monotonic() + (e.retry_after or 30)
                    elif e.kind == "config":
                        self.auth_failed.add(name)
                    break
                self._record(prov, role, comp, t0)
                self.last_used[name] = time.time()
                if name != route.primary:
                    self.bus.emit("MODEL", "FALLBACK", f"used {name} after {route.primary} failed")
                return comp
        raise AllProvidersFailed(attempts)

    def _record(self, prov: ModelProvider, role: str, comp: Completion | None, t0: float, error: str = "") -> None:
        u = comp.usage if comp else None
        cost = prov.cost(u) if u else 0.0
        row = (time.time(), self.project_name, self.session_id, prov.name, prov.cfg.model, role,
               u.input_tokens if u else 0, u.output_tokens if u else 0, u.cached_tokens if u else 0,
               cost, time.time() - t0, 1 if comp else 0, error[:300])
        sql = ("INSERT INTO model_calls(ts,project,session_id,provider,model,role,input_tokens,output_tokens,cached_tokens,"
               "cost,latency,ok,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)")
        for st in {id(self.gstore): self.gstore, id(self.pstore): self.pstore}.values():
            st.execute(sql, row)

    # -- human override -------------------------------------------------------
    def pin(self, target: str, provider: str) -> str:
        if provider not in self.configs():
            raise KeyError(provider)
        self.pins[target] = provider
        return f"{target} → {provider}"

    def context_usage(self, tokens_used: int, role: str = "implementer") -> float:
        r = self.last_route or self.route(role)
        if not r.primary:
            return 0.0
        return min(1.0, tokens_used / max(1, self.configs()[r.primary].context_window))
