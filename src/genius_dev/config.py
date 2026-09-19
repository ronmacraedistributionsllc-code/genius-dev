"""Global (~/.genius-dev/config.toml) and per-project (.genius/config.toml) configuration."""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .secrets import DEFAULT_EXCLUDED_DIRS


def global_dir() -> Path:
    """Genius Dev's own directory. Deliberately NOT ``~/.genius`` (another Genius tool already uses that name); override with GENIUS_HOME."""
    d = Path(os.environ.get("GENIUS_HOME") or Path.home() / ".genius-dev")
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULT_CONFIG: dict[str, Any] = {
    "general": {
        "permission": "standard",      # safe | standard | autonomous
        "routing_mode": "balanced",    # economy | balanced | max_quality | local_only | custom
        "daily_budget": 0.0,           # USD, 0 = unlimited
        "onboarded": False,
        "editor": "",
    },
    "routing": {"custom": {}},          # role -> provider name (used in `custom` mode; overrides in any mode)
    "fallback": {"chain": []},          # provider names tried in order after the routed one
    "privacy": {
        "cloud_allowed": True,
        "excluded": DEFAULT_EXCLUDED_DIRS,
        "local_only_files": [],
        "max_external_context_chars": 120_000,
        "redact_secrets": True,
    },
    "protect": {"paths": []},
    "ui": {"animations": True, "keys": {}},
    "browser": {"headless": True, "width": 1280, "height": 800},
    "git": {"auto_init": True, "auto_commit": False},
    "tools": {"shell_timeout": 300, "max_output_chars": 6000},
    "plugins": {"trust_project": False},
    "providers": {},
}


@dataclass
class ProviderConfig:
    name: str
    kind: str = "openai"              # mock | openai | anthropic | gemini | ollama | lmstudio | openrouter
    base_url: str = ""
    api_key_ref: str = "none"         # env:NAME | keychain:account | none
    model: str = ""
    context_window: int = 128_000
    input_cost: float = 0.0           # USD per 1M input tokens
    output_cost: float = 0.0          # USD per 1M output tokens
    cached_input_cost: float | None = None
    capabilities: list[str] = field(default_factory=lambda: ["tools", "structured"])
    enabled: bool = True
    tier: int = 2                     # 1 economy / 2 balanced / 3 strongest
    local: bool = False

    @staticmethod
    def from_dict(name: str, d: dict[str, Any]) -> "ProviderConfig":
        known = {k: v for k, v in d.items() if k in ProviderConfig.__dataclass_fields__ and k != "name"}
        return ProviderConfig(name=name, **known)

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "name"}
        return {k: v for k, v in d.items() if v is not None}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def dumps_toml(d: dict[str, Any], prefix: str = "") -> str:
    """Minimal TOML writer (scalars, lists, nested tables)."""
    lines: list[str] = []
    scalars = {k: v for k, v in d.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in d.items() if isinstance(v, dict)}
    if prefix and (scalars or not tables):
        lines.append(f"[{prefix}]")
    for k, v in scalars.items():
        lines.append(f'{_key(k)} = {_fmt(v)}')
    if prefix and (scalars or not tables):
        lines.append("")
    for k, v in tables.items():
        lines.append(dumps_toml(v, f"{prefix}.{_key(k)}" if prefix else _key(k)))
    return "\n".join(lines).rstrip() + "\n" if not prefix else "\n".join(lines)


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else f'"{k}"'


def load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return {}


class Config:
    """Merged view: defaults <- global <- project. Writes go to a chosen scope."""

    def __init__(self, project_root: Path | None = None):
        self.project_root = project_root
        self.global_path = global_dir() / "config.toml"
        self.project_path = (project_root / ".genius" / "config.toml") if project_root else None
        self.reload()

    def reload(self) -> None:
        self._global = load_toml(self.global_path)
        self._project = load_toml(self.project_path) if self.project_path else {}
        self.data = _merge(_merge(DEFAULT_CONFIG, self._global), self._project)

    # -- access ---------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any, scope: str = "global") -> None:
        store = self._project if scope == "project" and self.project_path else self._global
        cur = store
        parts = dotted.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
        self._save(scope)
        self.reload()

    def _save(self, scope: str) -> None:
        path, data = (
            (self.project_path, self._project) if scope == "project" and self.project_path else (self.global_path, self._global)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps_toml(data))

    def providers(self) -> dict[str, ProviderConfig]:
        return {n: ProviderConfig.from_dict(n, d) for n, d in self.data.get("providers", {}).items()}

    def save_provider(self, p: ProviderConfig, scope: str = "global") -> None:
        self.set(f"providers.{p.name}", p.to_dict(), scope)

    @property
    def permission(self) -> str:
        return self.get("general.permission", "standard")

    @property
    def mode(self) -> str:
        return self.get("general.routing_mode", "balanced")


PRESETS: dict[str, dict[str, Any]] = {
    # Base URLs / defaults are starting points; model IDs and prices are user-editable.
    "anthropic": {"kind": "anthropic", "base_url": "https://api.anthropic.com", "api_key_ref": "env:ANTHROPIC_API_KEY",
                  "model": "claude-sonnet-5", "context_window": 200_000, "capabilities": ["tools", "structured", "images"], "tier": 3},
    "openai": {"kind": "openai", "base_url": "https://api.openai.com/v1", "api_key_ref": "env:OPENAI_API_KEY",
               "model": "", "context_window": 128_000, "capabilities": ["tools", "structured", "images"], "tier": 3},
    "gemini": {"kind": "gemini", "base_url": "https://generativelanguage.googleapis.com", "api_key_ref": "env:GEMINI_API_KEY",
               "model": "", "context_window": 1_000_000, "capabilities": ["tools", "structured", "images"], "tier": 3},
    "openrouter": {"kind": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key_ref": "env:OPENROUTER_API_KEY",
                   "model": "", "context_window": 128_000, "tier": 2},
    "deepseek": {"kind": "openai", "base_url": "https://api.deepseek.com/v1", "api_key_ref": "env:DEEPSEEK_API_KEY",
                 "model": "", "context_window": 64_000, "tier": 1},
    "qwen": {"kind": "openai", "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "api_key_ref": "env:DASHSCOPE_API_KEY",
             "model": "", "context_window": 128_000, "tier": 1},
    "ollama": {"kind": "ollama", "base_url": "http://localhost:11434", "api_key_ref": "none", "model": "",
               "context_window": 32_000, "local": True, "tier": 1},
    "lmstudio": {"kind": "lmstudio", "base_url": "http://localhost:1234/v1", "api_key_ref": "none", "model": "",
                 "context_window": 32_000, "local": True, "tier": 1},
    "mock": {"kind": "mock", "base_url": "", "api_key_ref": "none", "model": "genius-mock-v1", "context_window": 100_000,
             "capabilities": ["tools", "structured"], "tier": 2, "local": True},
}


# Display order and labels for the provider catalog (what `genius models` and the Models screen list).
CATALOG = ["anthropic", "qwen", "openai", "deepseek", "gemini", "openrouter", "ollama", "lmstudio", "mock"]
LABELS = {"anthropic": "Anthropic", "qwen": "Qwen", "openai": "OpenAI", "deepseek": "DeepSeek", "gemini": "Gemini", "openrouter": "OpenRouter",
          "ollama": "Ollama", "lmstudio": "LM Studio", "mock": "Mock"}


def seed_default(cfg: Config) -> bool:
    """First run: make the offline mock provider available so the app is fully usable with no account. Returns True if seeded."""
    if cfg.providers():
        return False
    cfg.save_provider(ProviderConfig.from_dict("mock", PRESETS["mock"]))
    return True
