"""Long-running dev process manager (servers, workers) with logs, port tracking and cleanup."""
from __future__ import annotations

import asyncio
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from .db import Store


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


class ProcessManager:
    def __init__(self, store: Store, root: Path, log_dir: Path):
        self.store, self.root, self.log_dir = store, root, log_dir
        log_dir.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[dict[str, Any]]:
        rows = []
        for r in self.store.query("SELECT * FROM processes ORDER BY started"):
            d = dict(r)
            alive = pid_alive(d["pid"])
            if not alive and d["status"] == "running":
                self.store.execute("UPDATE processes SET status='exited' WHERE name=?", (d["name"],))
                d["status"] = "exited"
            rows.append(d)
        return rows

    def get(self, name: str) -> dict[str, Any] | None:
        return next((p for p in self.list() if p["name"] == name), None)

    async def start(self, name: str, command: str, port: int = 0, ready_timeout: float = 30) -> dict[str, Any]:
        cur = self.get(name)
        if cur and cur["status"] == "running":
            return {**cur, "reused": True}                 # never start a duplicate
        if port and port_in_use(port):
            raise RuntimeError(f"port {port} is already in use by another process")
        log = self.log_dir / f"{name}.log"
        fh = open(log, "wb")
        proc = subprocess.Popen(command, shell=True, cwd=str(self.root), stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True,
                                env={**os.environ, "FORCE_COLOR": "0", "BROWSER": "none"})
        url = f"http://localhost:{port}" if port else ""
        self.store.execute(
            "INSERT INTO processes(name,pid,command,port,url,log,started,status) VALUES(?,?,?,?,?,?,?,'running') "
            "ON CONFLICT(name) DO UPDATE SET pid=excluded.pid,command=excluded.command,port=excluded.port,url=excluded.url,"
            "log=excluded.log,started=excluded.started,status='running'",
            (name, proc.pid, command, port, url, str(log), time.time()))
        deadline = time.time() + ready_timeout
        while port and time.time() < deadline:
            if proc.poll() is not None:
                self.store.execute("UPDATE processes SET status='exited' WHERE name=?", (name,))
                raise RuntimeError(f"process exited early (code {proc.returncode}). Log tail:\n{self.tail(name, 15)}")
            if port_in_use(port):
                break
            await asyncio.sleep(0.25)
        else:
            if port:
                detected = self.detect_port(name)
                if detected and detected != port:
                    self.store.execute("UPDATE processes SET port=?, url=? WHERE name=?", (detected, f"http://localhost:{detected}", name))
        return {**(self.get(name) or {}), "reused": False}

    def detect_port(self, name: str) -> int:
        m = re.findall(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})", self.tail(name, 60))
        return int(m[-1]) if m else 0

    def tail(self, name: str, lines: int = 40) -> str:
        p = self.log_dir / f"{name}.log"
        try:
            return "\n".join(p.read_text(errors="replace").splitlines()[-lines:])
        except FileNotFoundError:
            return ""

    def stop(self, name: str) -> bool:
        p = self.get(name)
        if not p:
            return False
        if pid_alive(p["pid"]):
            try:
                os.killpg(os.getpgid(p["pid"]), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            for _ in range(20):
                if not pid_alive(p["pid"]):
                    break
                time.sleep(0.1)
            else:
                try:
                    os.killpg(os.getpgid(p["pid"]), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.store.execute("UPDATE processes SET status='stopped' WHERE name=?", (name,))
        return True

    def stop_all(self) -> int:
        n = 0
        for p in self.list():
            if p["status"] == "running":
                n += self.stop(p["name"])
        return n

    async def restart(self, name: str) -> dict[str, Any]:
        p = self.get(name)
        if not p:
            raise KeyError(name)
        self.stop(name)
        return await self.start(name, p["command"], p["port"])
