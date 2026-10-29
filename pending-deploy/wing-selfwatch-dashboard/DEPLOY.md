# wing-selfwatch-dashboard

Surfaces wing node self-watchdog events in the dashboard "Eventos" modal.
Before this, the modal showed only the main-node `df-watchdog.log` — node
recoveries (dead-helper restarts, container starts, disk prunes, log trims,
drift alerts on vultr) were invisible overnight.

## What changed
- `wing/agent.py` 1.0.8 -> 1.0.9: NEW authed
  `GET /api/agent/selfwatch-events?lines=N` (X-Agent-Key) tailing the fixed
  `SELF_WATCH_DIR/selfwatch.log` (last 64KB, lines clamped 1..200, 500-char
  cap; lines returned RAW, the manager applies its canonical severity
  tagging). Missing log -> `{"ok": False}`.
- `manager/app.py`: NEW `_event_ts()` (parses the `[YYYY-MM-DD HH:MM:SS]`
  prefix), NEW `fetch_node_watchdog_events()` (per recently-heartbeated
  provider, attempts=1 fail-fast, 5s timeout; offline/old-agent nodes
  contribute nothing; node name truncated 40 chars), NEW
  `merge_watchdog_events()` (chronological merge, stable, ts stripped
  after). `/api/watchdog-events` now returns
  `{"ok","path","nodes", "events":[{"line","sev","node"}]}` —
  backward compatible (old keys unchanged, new fields additive).
- `manager/static/index.html`: Eventos modal renders a `.ev-node` chip per
  node event (textContent-only, no innerHTML), meta line shows the
  contributing node count.

## Tests
`python3 test_wing_selfwatch_dashboard.py` — 72/72 hermetic checks
(agent tail/clamp/sev-contract/route auth-gating, manager ts/fetch/merge,
route wiring, dashboard contract incl. textContent-only). No network/docker.

## Deploy (SSH-capable session only)
Rides `apply-all` from repo HEAD (agent.py + app.py + index.html are
already in the stage list):

```bash
bash ~/workspace/droidfarm/pending-deploy/apply-all/deploy-all.sh
```

Standalone equivalent:
```bash
scp ~/workspace/droidfarm/wing/agent.py root@209.222.10.93:/root/jerusalem/agent-stage/
scp ~/workspace/droidfarm/manager/app.py ~/workspace/droidfarm/manager/static/index.html root@209.222.10.93:/root/jerusalem/
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify
- `curl -sS -m 8 -H "X-Agent-Key: <key>" 'http://<vultr>:8090/api/agent/selfwatch-events?lines=5'` ->
  `{"ok": true, "events": [...]}` (after Penta re-runs the node installer).
- Dashboard: open Eventos, expect events with a node chip + meta "N eventos · … · M nodo(s)".
- Old agents (pre-1.0.9) answer 404 -> manager silently contributes
  nothing (no errors in Eventos).

## Blocked
- Agent 1.0.9 must be rolled out on the vultr node — Penta re-runs the
  node installer there (same re-run already pending for 1.0.2-1.0.8).
  Until then srt events simply don't appear; nothing misreports.
- No VPS SSH in worker sessions -> deploy staged.
