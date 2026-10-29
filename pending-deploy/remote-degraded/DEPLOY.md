# remote-degraded — per-instance selfwatch DEGRADED state on wing-node cards

## Problem

The wing agent's selfwatch (1.0.3+) writes per-instance `<name>.degraded`
markers when its circuit breaker pauses auto-recovery (container restart
storm, helper crash loop, stale screencap-loop storm, Waydroid session-death
storm). That state never reached the dashboard's instance cards: the
manager's `/api/instances` remote branch hardcoded `"degraded": False` with
the stale comment "watchdog only runs on the main node" — so a wedged
remote instance (e.g. srt1 with auto-recovery paused) kept showing a green
"Ready" card. The node-level "needs attention" badge in the Nodes modal
existed, but the per-instance card — what Penta actually watches — lied.
This is the twelfth "main had it, wing didn't" parity item (the local
branch got the same fix in 2026-10-07's instance-health package).

## What changed

- `wing/agent.py` **1.0.21 → 1.0.22**: new `read_selfwatch_degraded(name,
  st_dir=None)` — reads the `<name>.degraded` marker (reason on the first
  line, truncated to 160, same name-gate as `read_roblox_drift`, never
  raises). `instance_status_linux()` reports `degraded` / `degraded_reason`
  on both the running and stopped branches — rides the 1.0.20 status-cache
  snapshot, so both `do_GET` branches (fleet list + single status) carry it
  with zero route changes.
- `manager/app.py`: new `clean_agent_degraded()` (strict-True, older agents
  omit the field → False) and `clean_agent_degraded_reason()` (str/strip/
  160-cap, else None), following the `clean_agent_drift*` pattern. The
  `/api/instances` remote branch fills `degraded` / `degraded_reason` from
  the agent instead of the hardcoded `False`/`None`; the stale "watchdog
  only runs on the main node" comment is gone.
- Dashboard: **no change needed** — the card badge already reads
  `i.degraded` / `i.degraded_reason`, with priority over the setup and
  drift badges, and the grid auto-refreshes while any card is degraded.

## Deploy

Rides `apply-all` from HEAD — `wing/agent.py` + `manager/app.py` are already
in the stage list. After deploy, the vultr node needs Penta's installer
re-run for agent 1.0.22 (until then remote cards keep showing the old
always-green state, which is today's behavior — nothing misreports).

## Verify

- `python3 pending-deploy/remote-degraded/test_remote_degraded.py`
  → `46 passed, 0 failed`
- Full `deploy-all.sh --stage-only` dry run green (preflight t79,
  queue-audit 0 findings)

## Tests

`test_remote_degraded.py` (hermetic): VERSION exact 1.0.22 + manager
`>=1.0.2` gate, `read_selfwatch_degraded` units (marker/multiline/
truncation/hostile names/unreadable dir/never-raises),
`instance_status_linux` running + stopped branches (fake docker),
manager sanitizer units, `/api/instances` remote-branch source contract
(stale comment gone, no hardcoded pair left), dashboard contract (badge,
priority, auto-refresh), status-cache passthrough.
