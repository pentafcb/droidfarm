# wing-keepalive-mirror

Agent 1.0.14 -> 1.0.15: mirrors the in-container roblox-keepalive.sh v2
`keepalive.degraded` marker into the agent's instance `.degraded` marker
(main-node df-watchdog check 3b parity), so a crash-looping Roblox on a wing
node surfaces on the dashboard Nodes "needs attention" badge instead of
silently backing off with a green node.

- `sw_keepalive_marker_path()` — host-side volume path for the marker, None
  on invalid names, never raises.
- `sw_check_keepalive_degraded()` — one pass: "degraded" (mirror written or
  already present), "ok" (no marker), "skip" (invalid/unreadable, never
  raises). Reason prefixed `Roblox keepalive: `, truncated to 160 chars;
  pre-existing `.degraded` markers never clobbered; exactly one DEGRADED log
  line per mirror (no spam on repeat passes).
- Wired into `selfwatch_check_instance()` as check 3b (after the stale-stream
  check, before the degraded auto-clear): degraded -> `healthy = False`, so
  the auto-clear waits for the keepalive to recover. Never touches the
  circuit-breaker budget — surfacing, not auto-recovery.
- Visibility: the mirror flows through the existing `selfwatch_state()`
  degraded map -> heartbeat -> dashboard Nodes badge; zero manager/dashboard
  changes.

Tests: `test_wing_keepalive_mirror.py` — 35/35 hermetic (fake docker +
fake volume tree): version + manager-gate compat, path units, ok/degraded/
skip semantics, mirror content, no-log-spam, no-clobber, default + truncated
reasons, full-pass wiring (unhealthy + mirror), recovery auto-clear, never-
raises, 3b placement contract, degraded-map visibility, constants.
