"""Design tokens shared by the TUI and CLI output. One restrained accent; semantic colours only for state."""
from __future__ import annotations

from rich.text import Text

BG = "#0d0e11"
SURFACE = "#14161a"
TEXT = "#e7e8ea"
MUTED = "#8b9099"
DIM = "#565b64"
RULE = "#23262c"
ACCENT = "#8ab4ff"
AI = "#a99bff"
OK = "#5fd08a"
FAIL = "#f26d6d"
WARN = "#e5c07b"

STATUS_STYLE = {
    "PASS": (OK, "✓"), "FAIL": (FAIL, "✕"), "BLOCKED": (WARN, "⊘"), "WAIVED": (MUTED, "–"),
    "IN_PROGRESS": (ACCENT, "●"), "NOT_STARTED": (DIM, "○"),
    "done": (OK, "✓"), "active": (ACCENT, "●"), "todo": (DIM, "○"), "blocked": (WARN, "⊘"), "skipped": (MUTED, "–"),
}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def bar(pct: float, width: int = 32, color: str = ACCENT) -> Text:
    """Thin progress bar using half-height blocks; track is a dim rule colour."""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100))
    t = Text()
    t.append("━" * filled, style=color)
    t.append("━" * (width - filled), style=RULE)
    return t


def money(v: float) -> str:
    return f"${v:,.2f}" if v >= 0.01 or v == 0 else f"${v:.4f}"


def label(text: str) -> Text:
    return Text(text.upper(), style=f"bold {DIM}")


def status_mark(status: str) -> Text:
    color, glyph = STATUS_STYLE.get(status, (MUTED, "·"))
    return Text(glyph, style=color)
