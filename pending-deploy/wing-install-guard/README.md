# wing-install-guard — agent 1.0.35 Roblox install-failure surfacing

The wing-side counterpart of the roblox-install-guard (df-watchdog.sh v15
check 3c). The same `waydroid-instance:latest` image runs on wing nodes, so
its `boot-complete.sh` writes `roblox_install_failed` (reason on first line)
into the data volume when the one-shot `pm install` fails, times out, or
the APKs are missing — but the wing agent's selfwatch had no 3c check, so a
failed install on a node instance sat on "installing_roblox" forever with a
green card.

## What changed

`wing/agent.py` 1.0.34 → 1.0.35:

- `SW_INSTALL_FAILED_MARKER = "roblox_install_failed"` +
  `sw_install_failed_marker_path()` — same host-side data-volume layout as
  the 1.0.15 keepalive mirror (`wd-<name>-data/_data/data/media/0/jerusalem/`),
  invalid names → `None`, never raises.
- `sw_check_install_failed()` wired as check 3c right after the 3b keepalive
  mirror in `selfwatch_check_instance()`: marker present → writes
  `<name>.degraded` ("Roblox install failed: \<reason\>" on the first line,
  160-char cap — the main-node v15 prefix, for cross-node consistency in the
  Eventos modal) + exactly one DEGRADED log line; instance counts as
  unhealthy so the step-4 auto-clear waits for boot-complete to clear its
  own marker on a later successful run.

Surfacing only: never routed through the circuit-breaker budget, never
clobbers a pre-existing marker, nothing restarted. Pre-v15 images never
write the volume marker, so the check is a silent no-op there. Zero
manager/dashboard changes — the mirror flows through `selfwatch_state()` →
heartbeat → the dashboard Nodes "needs attention" badge. The manager's
`>=1.0.2` agent gates accept 1.0.35 unchanged.

## Tests

`test_wing_install_guard.py` — 34/34 hermetic (real agent import, fake
volume tree + fake docker): exact VERSION bump + manager gate, marker-path
units (valid/invalid/never-raises), ok/degraded/skip semantics,
first-line-only reason, single DEGRADED log + no repeat spam, pre-existing
marker never clobbered, empty-marker "unknown error" default, 160-char
truncation, full-pass wiring (marker → unhealthy; absent → healthy), boot-
complete recovery auto-clear + "recovered" log, invalid/unreadable never
raises, 3c placement (after 3b, before step 4) + no-`cb_guard` contracts,
degraded-map visibility through `selfwatch_state()`, the constant.

## Wiring

- Preflight t109 (`PASS=34 FAIL=0` exact-count).
- 32 existing suites' VERSION tuples widened to 1.0.35 (counts unchanged);
  wing-start-route-dedup's exact-bump widened to 1.0.34+ per the handoff
  pattern (exact 1.0.35 owned by the new suite); remote-autologin
  app.py/agent.py snapshots refreshed to HEAD.
- Rides apply-all from HEAD (`wing/agent.py` already staged).
