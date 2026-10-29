# watcher-probe-timeout

## Problem
`booted-watcher.sh` and `entrypoint.sh` called `waydroid shell`
unbounded. A wedged Waydroid session hangs those calls forever:

- The watcher (long-lived loop, started by the entrypoint) froze on the
  hung probe → the late-boot `booted` marker was never written →
  dashboard stuck on "booting" → watchdog stuck-boot check restarted the
  container at 15 min (an unnecessary restart of a possibly-recovering box).
- The entrypoint's 60-iteration boot-wait loop never advanced past a hung
  first probe → the documented 5-min bound was defeated.

This is the same hang class that `keepalive-probe-timeout` (roblox-keepalive
v3) and `watchdog-timeout` (df-watchdog v13) fixed on their surfaces.

## Fix
- `waydroid-image/booted-watcher.sh`: new `bw()` wrapper — runs
  `waydroid shell` under `timeout` (`BOOTED_WATCHER_PROBE_TIMEOUT_S`,
  default 30s, env-overridable; falls back to the plain call when the
  `timeout` binary is missing). Both probes (`getprop sys.boot_completed`,
  `pm list packages`) go through it. A timeout (rc 124) logs one WARN and
  skips the cycle — it is UNKNOWN, never "not booted", so no marker is
  written and `boot-complete.sh` is not run on a wedged session.
- `waydroid-image/entrypoint.sh`: the boot-wait probe is wrapped in
  `timeout 10`; a hung probe retries next iteration and the 5-min bound
  actually holds.

`boot-complete.sh` itself is intentionally NOT bounded — it is the
long install operation, idempotent, and safe to retry.

## Tests
`test_watcher_probe_timeout.sh` — 26/26 hermetic (fake `waydroid` with
hang modes, real scripts, outer `timeout` guards so a broken product
fails instead of hanging the suite):
- hung getprop/pm probes bounded (~2s vs 120s), no marker, WARN, exit 0
- normal probes unchanged (marker + roblox_done, no spurious WARN)
- fast failures stay silent (no timeout WARN)
- no-`timeout`-binary fallback works
- garbage timeout env tolerated
- `bw()` unit: 124 on timeout, rc passthrough otherwise
- entrypoint: source contract + functional loop run against hanging
  fake (completes, BOOTED=0)
- steady state (both markers present) still probes nothing

Preflight t96 (exact-count "26 passed, 0 failed").
