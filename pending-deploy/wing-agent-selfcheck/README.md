# wing-agent-selfcheck

Agent 1.0.17: HTTP self-check for wing nodes — main-node df-watchdog v5/v12
parity.

## The gap
The main node's watchdog restarts the jerusalem manager when its HTTP
server stops answering (v5, probe hardened in v12). Wing agents had no
equivalent: a wedged agent HTTP server is invisible to systemd and stops
heartbeats, screenshots, creates and logins while the process looks alive.

## What it does
Once per selfwatch pass (60s), the agent probes its own
`/api/agent/health` (unauthenticated by design) and requires a complete
non-empty body with a 2xx/3xx status — a truncated 200 counts as failed
(v12 parity). Two consecutive failures → one ALERT line in selfwatch.log
+ process exit; systemd `Restart=always` revives it in ~5s. More than 2
self-exits in an hour → `agent.http.degraded` marker (reason on the first
line), alert-only until a healthy probe clears it.

Guards:
- Skipped while the server is not bound yet (`_AGENT_HTTP_BOUND`, set by
  `main()` after the bind — no startup-race false strikes).
- Skipped when not running under systemd (`INVOCATION_ID`/`JOURNAL_STREAM`
  absent) — one WARN per day, never silent. Exiting there would orphan the
  node.
- Windows: the whole selfwatch is Linux-only already.

## Visibility (zero manager/dashboard changes)
- `agent.http.degraded` → `selfwatch_state()` degraded map → heartbeat →
  dashboard Nodes "needs attention" badge.
- ALERT / DEGRADED / recovered lines → selfwatch.log →
  `/api/agent/selfwatch-events` → the dashboard Eventos modal.

## Tests
`test_wing_agent_selfcheck.py` — 71/71 hermetic checks: systemd detection,
real-socket probe behavior (healthy / truncated / 500 / empty / refused),
the full strike→exit→degraded state machine, budget exhaustion, recovery,
never-raises, wiring contracts, degraded-map visibility.
