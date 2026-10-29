# boot-install-timeout — DEPLOY

## Problem

`waydroid-image/boot-complete.sh` ran three **foreground** `waydroid shell`
calls unbounded:

1. `pm list packages` (Roblox install check),
2. `pm install` of the ~150MB split APKs,
3. `input keyevent 4` (dismiss).

A wedged Waydroid session hangs `waydroid shell` forever. The hang wedged
the whole boot sequence **after** the `booted` marker was written, so:

- the dashboard showed "ready" (not "booting"),
- the stuck-boot check never fired (marker present),
- the session-death check never fired (session alive),
- the instance sat with no helpers and no Roblox — a silent wedge.

The watchdog's missing-script guards (WARN/day, no budget burn) kept it
from tripping a misleading DEGRADED, but nothing recovered it.

## What it does

`waydroid-image/boot-complete.sh` only (image-side):

- New `bcs()` wrapper: `bcs <secs> <waydroid args...>` runs `waydroid`
  under GNU `timeout` (present in this image), falling back to the plain
  call when the binary is missing. Same pattern as keepalive v3's `aw()`
  and the watcher's `bw()`.
- All three foreground calls bounded: `pm list` 30s, `pm install` 300s
  (generous for ~150MB on a loaded box), keyevent 15s. Timeouts are
  env-overridable (`BC_PM_LIST_TIMEOUT_S`, `BC_PM_INSTALL_TIMEOUT_S`,
  `BC_KEYEVENT_TIMEOUT_S`).
- A timeout (rc 124) is UNKNOWN, not failure: distinct WARN naming the
  cause ("wedged session?"), and the script **continues** — the helper
  scripts are still installed and started. Without `roblox_done` the
  dashboard honestly shows "installing_roblox" (not "ready"); a container
  restart re-runs boot-complete.sh and retries the install.
- Backgrounded calls (`nohup ... &`) untouched — they cannot wedge the script.

## Tests

`test_boot_install_timeout.sh` — 38/38 pass (hermetic, fake waydroid with
hang modes; outer `timeout` guards every run so a broken product fails
instead of hanging the suite):

- T1 happy-path regression (install, markers, helpers, launch taps).
- T2 `pm list` hang → bounded, install still attempted, completes.
- T3 `pm install` hang → bounded, timeout WARN, no `roblox_done`,
  helpers STILL installed and started (the fix).
- T4 keyevent hang → bounded, WARN, completes.
- T5 no `timeout` binary → fallback, normal path works.
- T6 garbage timeout env → no hang, completes.
- T7 source contracts (three calls bounded, backgrounded untouched, no
  raw foreground `waydroid shell`).
- T8 `bcs()` unit (124 on timeout, passthrough, fallback).

Regression: `test_boot_complete.sh` 63/63 still green.

## Deploy

Image-side. No apply-all staging (boot-complete.sh is not in the stage
list — same as the boot-complete package).

1. The fix rides the next `vps-image-build.sh` run via the Dockerfile
   `COPY boot-complete.sh /opt/boot-complete.sh` (the derived preflight
   validates the COPY set; no preflight change needed).
2. Containers created/restarted from the new image get the bounded
   install block at boot. There is no live-container push (one-shot boot
   script, not a loop).
3. Verify on the VPS after the image build:
   `docker run --rm --entrypoint sh waydroid-instance:latest -c "grep -q '^bcs() {' /opt/boot-complete.sh && echo BOUNDED"`.

## Rollback

Rebuild the image from the previous commit. The change is strictly
additive (timeouts + WARNs); worst case without it is the old silent
wedge, which the watchdog's missing-script WARNs still surface.
