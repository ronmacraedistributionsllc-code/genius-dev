# Model routing, fallback and budget (`router.py`)

Each agent role maps to a *category* and a preferred strength:

| Role | Category | Prefers |
|---|---|---|
| planner, architect | planning / architecture | strongest |
| debugger, reviewer, final_reviewer, security_reviewer, ui_reviewer | debugging / review / security / UI critique | strongest (`ui_reviewer` also requires `images`) |
| implementer, tester | coding / test generation | economical |
| summarizer, docs, commit | documentation | economical |

Modes (`general.routing_mode`, `genius model --mode`, palette): **economy** (cheapest everywhere except planner/architect),
**balanced** (default: strongest for the roles above, economical otherwise), **max_quality**, **local_only**
(only providers with `local = true`), **custom** (`[routing.custom] implementer = "qwen"`; custom entries apply in any mode).
Human overrides (`use qwen for coding`, `genius model implementer --use qwen`) pin a role and are always explained. `genius models set-primary <name>` makes one provider the default for every role (still subject to local-only, budget and privacy filters; per-role pins and custom routes win over it), and `set-fallback a b c` fixes the order tried when it fails. `genius model` (no argument) prints the routing decision for every role.

Every route carries its reasoning: `genius model planner` → task, selected, fallback, reason (also shown in the Models view and
`MODEL ROUTE` events). Providers are ranked by `(tier, price)`; providers whose context window is smaller than the request are skipped.

**Fallback** (`Router.complete`): the routed provider, then `[fallback] chain`, then remaining providers by strength.
`rate_limit` → wait ≤5 s once, then next provider and a cooldown; `outage` → cooldown and next; `auth`/`config` → provider is
disabled for the session and never retried (no endless loops with bad credentials); `context` → next larger window. At most 2
attempts per provider; when everything fails `AllProvidersFailed` lists every attempt.

**Budget** (`genius budget 5`): spend today is summed from `model_calls` using your configured per-1M prices.
≥80% → new non-critical calls are restricted to local / cheaper providers. ≥100% → cloud providers are refused for new work
(local models still work; with none available the run ends with `BudgetExceeded` and a clear message). The implementer loop
is `critical`, so an in-flight edit sequence may finish with a 10% grace for estimate drift; beyond 110% everything cloud stops.

**Privacy** applied per call to non-local providers: secrets redacted, `privacy.max_external_context_chars` enforced (oldest
turns dropped first), and `privacy.cloud_allowed = false` forces local-only.
