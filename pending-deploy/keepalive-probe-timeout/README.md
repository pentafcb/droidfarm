# keepalive-probe-timeout

Keepalive v3: bounded Android probes + unknown-skip + wedged-session
surfacing for the in-container Roblox keepalive loop.

## Problem
`roblox-keepalive.sh` v2 called `dumpsys window` / `pm path` unbounded.
A wedged Android session hangs those calls forever: the loop froze inside
`roblox_focused()`, `pgrep` still saw the script alive (no watchdog
restart), `keepalive.degraded` was never written (no dashboard badge),
and Roblox sat unfocused behind a green card indefinitely.

## Fix
- `aw()` runs every probe and every launch `input` under
  `timeout $KA_PROBE_TIMEOUT_S` (default 10s), with a no-timeout fallback.
- A timed-out probe is UNKNOWN: skip the launch cycle, don't burn the
  launch-attempt budget.
- 3 consecutive focus-probe timeouts (`KA_PROBE_FAILS`) write
  `keepalive.degraded` with a probe-timeout reason on the first line —
  mirrored to the dashboard badge by the existing df-watchdog v8 check 3b
  / wing agent 1.0.15 check 3b. Zero manager/dashboard changes.
- Recovery (Roblox focused) clears the marker; a `pm` timeout while
  dumpsys answers skips conservatively without touching the counter.

## Tests
`test_keepalive_probe_timeout.sh` — 32 checks, hermetic (fake Android
tools with hang modes, real script, `KA_*` seams).

## Wiring
- Preflight t95 in `pending-deploy/apply-all/deploy-all.sh`.
- No new staged files: `waydroid-image/roblox-keepalive.sh` already rides
  apply-all from HEAD; `push-keepalive.sh` (finish-deploy.sh step 1)
  pushes it into live containers; the Dockerfile `COPY` covers new images.
