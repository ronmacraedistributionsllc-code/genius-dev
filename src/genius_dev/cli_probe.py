"""Real provider probes shared by the CLI, doctor and TUI. A cloud provider with no credential is never contacted."""
from __future__ import annotations

import asyncio

from .providers.status import record_live
from .secrets import has_key

LOCAL = ("mock", "ollama", "lmstudio")


async def probe_providers(router, names: list[str] | None = None, record: bool = True, local_only: bool = False) -> dict[str, tuple[bool, float, str]]:
    cfgs = router.configs()
    todo = []
    for n in names or cfgs:
        c = cfgs.get(n)
        if not c or not c.enabled or not c.model:
            continue
        is_local = c.kind in LOCAL or c.api_key_ref == "none"
        if local_only and not is_local:
            continue
        if is_local or has_key(c.api_key_ref):
            todo.append(n)
    res = await asyncio.gather(*[router.provider(n).health_check() for n in todo])
    out = dict(zip(todo, res))
    if record:
        for n, (ok, lat, det) in out.items():
            if cfgs[n].kind not in LOCAL:
                record_live(router.gstore, n, ok, lat, det)
    return out
