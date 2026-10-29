# instance-health-cache

Per-instance docker snapshot cache (10s TTL) for the manager's
`GET /api/instances` poll path.

## Problem

Every dashboard poll ran one `docker inspect` per local instance (plus
marker-file reads and the watchdog health assembly). On a 20-instance
fleet that's 20 serialized subprocess spawns per poll, per client —
seconds of added latency on every grid refresh.

## Change

- `manager/app.py`: new `instance_snapshot(name, inst)` — one cached
  snapshot per instance (`status`, `ip`, `setup`, `degraded`,
  `degraded_reason`, `roblox_drift`, `roblox_drift_reason`), 10s TTL,
  keyed by sanitized instance name. One `docker inspect` per instance per
  10s window, matching the dashboard's own refresh cadence.
- `GET /api/instances` local branch uses the snapshot; response fields
  unchanged.
- `invalidate_instance_snapshot(name)` called on start/stop/restart
  (shared by single-instance endpoints and the fleet batch worker) and on
  delete, so the next poll shows fresh state.
- All helpers best-effort, never raise: worst case degrades to old
  behavior (fresh snapshot per poll), never a broken response.
- Tests: 37/37 pass (cache hit/miss/TTL, content, never-raises,
  invalidation, threadsafety, wiring contract).

## Deploy

Rides apply-all from HEAD (`manager/app.py` already staged). Suite `t28`
in the preflight. Standalone scp + `systemctl restart jerusalem` commands
in DEPLOY.md. Blocked in worker sessions: no VPS SSH (one test per
session, no retries).
