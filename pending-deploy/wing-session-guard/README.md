# wing-session-guard — wing agent 1.0.11: Waydroid session-death guard

## The gap
The main-node watchdog learned to detect a dead Waydroid session inside a
running container in v9 (2026-10-08): the container stays up, the booted
marker persists, and the helper daemons run through `waydroid shell`, so a
dead session was invisible to every check — helpers died, their restarts
failed silently, and the only symptom was a stale stream burning circuit
budgets until a misleading DEGRADED.

Remote (wing) nodes never got that check. The agent's selfwatch restarted
dead helpers via `docker exec ... waydroid shell ...`, which fails silently
on a dead session — burning the helper budget (8/10min) until a misleading
`input-daemon.sh restarted 9 times` DEGRADED, with the root cause (dead
session: black stream, dead input, Roblox never relaunched) never repaired.

## What it does
`sw_check_session()` runs in `selfwatch_check_instance` as check 1c
(between the Roblox drift surfacing and the helper checks), every 60s,
Linux only:

- **Probe**: `docker exec <c> waydroid shell getprop sys.boot_completed`
  (same idiom as the entrypoint boot check). Only for previously-booted
  instances — first boot is not probed.
- **Death**: detached `docker exec -d <c> waydroid session start`, then an
  early return so the helper/stream checks don't burn their budgets on a
  known-dead session. 180s grace after each start covers the Android
  re-boot window.
- **Budget**: >4 starts/hour -> instance DEGRADED (reason on the marker's
  first line), auto-recovery pauses, auto-clears when all green again.
- **Visibility**: recoveries bump `recoveries.json` and log to
  `selfwatch.log` -> the dashboard Eventos modal; DEGRADED surfaces via
  the existing `selfwatch_state()` degraded map -> heartbeat -> the Nodes
  modal "needs attention" badge. Zero manager/dashboard changes.

All helpers best-effort, never raise. The `wing/agent.py` VERSION goes
1.0.10 -> 1.0.11; the manager's `>=1.0.2` gates are unaffected.

## Tests
`test_wing_session_guard.py` — 49 hermetic checks (fake docker): version
bump + gate compat, booted-marker paths, probe alive/dead/failing, skip
paths, alive no-op + grace-ts drop, dead -> detached start + log + recovery
bump, early return (no helper restart on death), grace (no second start,
"waiting" log), grace expiry, failed start (no grace ts, retry next
minute), budget trip -> DEGRADED (single log line, no 5th start), silence
while degraded, auto-clear (markers + grace ts removed, "recovered"
logged), hostile inputs (corrupt grace ts, docker down) never raise,
wiring contract (1c placement + early return), and the degraded map
reaching the heartbeat payload.
