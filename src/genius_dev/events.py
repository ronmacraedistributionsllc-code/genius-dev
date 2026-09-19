"""Structured event stream: activity feed, log panel, headless JSONL and persistence share one bus."""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .secrets import redact

CATEGORIES = ["AGENT", "TOOLS", "BUILD", "TESTS", "BROWSER", "MODEL", "ERRORS", "SYSTEM"]


@dataclass
class Event:
    category: str
    label: str
    message: str
    ts: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)
    detail: str = ""          # long text shown only when expanded

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

    @property
    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.ts))


class EventBus:
    def __init__(self, store=None, session_id: int | None = None, keep: int = 2000):
        self.store = store
        self.session_id = session_id
        self.buffer: deque[Event] = deque(maxlen=keep)
        self.subscribers: list[Callable[[Event], None]] = []

    def subscribe(self, cb: Callable[[Event], None]) -> None:
        self.subscribers.append(cb)

    def emit(self, category: str, label: str, message: str, detail: str = "", **data: Any) -> Event:
        ev = Event(category, label.upper(), redact(message), data=data, detail=redact(detail))
        self.buffer.append(ev)
        if self.store is not None:
            try:
                self.store.execute(
                    "INSERT INTO events(ts,session_id,category,label,message,data) VALUES(?,?,?,?,?,?)",
                    (ev.ts, self.session_id, category, ev.label, ev.message, json.dumps(data, default=str)[:4000]),
                )
            except Exception:
                pass
        for cb in list(self.subscribers):
            try:
                cb(ev)
            except Exception:
                pass
        return ev
