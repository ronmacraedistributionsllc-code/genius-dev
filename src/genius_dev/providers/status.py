"""Honest provider status. Green is reserved for states that were actually observed:

IMPLEMENTED / TESTED_WITH_MOCK  are properties of the code (every adapter has both).
READY FOR KEY                   cloud provider, no credential configured yet — implementation is ready.
KEY SET · UNTESTED              a credential exists but no live request has succeeded yet.
LIVE VERIFIED                   a real request succeeded (recorded with timestamp).
NOT CONFIGURED                  local provider with no model selected.
CONNECTED                       local/mock provider answered a real health check.
ERROR                           the last real check failed (detail explains why).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..config import CATALOG, LABELS, PRESETS, Config, ProviderConfig
from ..secrets import has_key

GREEN = {"CONNECTED", "LIVE VERIFIED"}
LOCAL_KINDS = {"ollama", "lmstudio", "mock"}


@dataclass
class ProviderStatus:
    name: str
    label: str
    kind: str
    model: str
    state: str
    detail: str = ""
    latency_ms: int | None = None
    configured: bool = False          # entry exists in config
    key_present: bool = False         # credential exists (never its value)
    primary: bool = False
    fallback: bool = False
    enabled: bool = True
    implemented: bool = True
    tested_with_mock: bool = True
    live_verified: bool = False
    verified_ts: float | None = None
    local: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def record_live(gstore, name: str, ok: bool, latency: float, detail: str) -> None:
    gstore.set_meta(f"live:{name}", {"ok": ok, "ts": time.time(), "latency": latency, "detail": detail[:200]})


def clear_live(gstore, name: str) -> None:
    gstore.execute("DELETE FROM meta WHERE key=?", (f"live:{name}",))


def last_live(gstore, name: str) -> dict | None:
    return gstore.get_meta(f"live:{name}")


def statuses(cfg: Config, gstore, probes: dict[str, tuple[bool, float, str]] | None = None) -> list[ProviderStatus]:
    """Combine config, credential presence, recorded live results and (optional) fresh probes for local/mock providers."""
    probes = probes or {}
    provs = cfg.providers()
    names = [n for n in CATALOG] + [n for n in provs if n not in CATALOG]
    primary = cfg.get("general.primary", "")
    chain = cfg.get("fallback.chain", []) or []
    out: list[ProviderStatus] = []
    for n in names:
        c: ProviderConfig | None = provs.get(n)
        preset = PRESETS.get(n, {})
        kind = c.kind if c else preset.get("kind", "openai")
        ref = c.api_key_ref if c else preset.get("api_key_ref", "none")
        local = (c.local if c else preset.get("local", False)) or kind in LOCAL_KINDS
        needs_key = ref != "none"
        key = has_key(ref) if needs_key else False
        model = c.model if c else ""
        st = ProviderStatus(n, LABELS.get(n, n), kind, model, "", configured=c is not None, key_present=key, primary=(n == primary), fallback=(n in chain),
                            enabled=c.enabled if c else True, local=local)
        live = last_live(gstore, n) if gstore else None
        probe = probes.get(n)
        if c is not None and not c.enabled:
            st.state, st.detail = "DISABLED", "disabled in config"
        elif kind == "mock":
            if probe:
                st.state, st.latency_ms = ("CONNECTED", round(probe[1] * 1000)) if probe[0] else ("ERROR", None)
                st.detail = "" if probe[0] else probe[2]
            else:
                st.state = "UNTESTED" if c else "NOT CONFIGURED"
        elif local:
            if c is None or not c.model:
                st.state, st.detail = "NOT CONFIGURED", "select a model: genius models select-model " + n + " <id>" if c else "genius models add " + n
            elif probe:
                st.state, st.latency_ms = ("CONNECTED", round(probe[1] * 1000)) if probe[0] else ("ERROR", None)
                st.detail = "" if probe[0] else probe[2]
            else:
                st.state = "UNTESTED"
        else:                                             # cloud / custom endpoint
            if needs_key and not key:
                st.state, st.detail = "READY FOR KEY", f"genius models key {n}"
            elif c is None:
                st.state, st.detail = "KEY SET · NOT ADDED", f"key found ({ref}); run: genius models add {n}"
            elif not c.model:
                st.state, st.detail = "NEEDS MODEL", f"genius models select-model {n} <id>"
            elif probe:
                st.state, st.latency_ms = ("LIVE VERIFIED", round(probe[1] * 1000)) if probe[0] else ("ERROR", None)
                st.detail = "" if probe[0] else probe[2]
            elif live:
                st.state = "LIVE VERIFIED" if live["ok"] else "ERROR"
                st.latency_ms = round(live["latency"] * 1000) if live["ok"] else None
                st.detail = "" if live["ok"] else live["detail"]
            else:
                st.state, st.detail = "KEY SET · UNTESTED", f"genius models test {n}"
        if st.state == "LIVE VERIFIED":
            st.live_verified = True
            st.verified_ts = (live or {}).get("ts") or time.time()
        out.append(st)
    return out
