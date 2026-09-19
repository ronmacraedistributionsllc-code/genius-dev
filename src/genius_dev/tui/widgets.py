"""Rich renderables used by TUI views and CLI output."""
from __future__ import annotations

import re

from rich.console import Group
from rich.text import Text

from .. import theme as T


def render_diff(diff: str, max_lines: int = 600) -> Group:
    """Subtle, readable diff: file headers in accent, hunks dim, +/- in semantic colours."""
    out: list[Text] = []
    n = 0
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            path = line.split(" b/")[-1]
            out.append(Text())
            out.append(Text(path, style=f"bold {T.ACCENT}"))
            continue
        if line.startswith(("index ", "--- ", "+++ ", "new file", "deleted file", "similarity")):
            if line.startswith(("new file", "deleted file")):
                out.append(Text(line, style=T.DIM))
            continue
        n += 1
        if n > max_lines:
            out.append(Text(f"… diff truncated at {max_lines} lines", style=T.DIM))
            break
        if line.startswith("@@"):
            out.append(Text(re.sub(r"^(@@[^@]*@@).*", r"\1", line), style=T.DIM))
        elif line.startswith("+"):
            out.append(Text(line, style=T.OK))
        elif line.startswith("-"):
            out.append(Text(line, style=T.FAIL))
        else:
            out.append(Text(line, style=T.MUTED))
    return Group(*out) if out else Group(Text("No changes.", style=T.MUTED))


def section(title: str, right: str = "") -> Text:
    t = Text(title.upper(), style=f"bold {T.DIM}")
    if right:
        t.append("  " + right, style=T.MUTED)
    return t


def hint(text: str) -> Text:
    return Text(text, style=T.DIM)


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"
