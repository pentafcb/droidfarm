# roblox-install-guard

## Problem
`boot-complete.sh` runs the one-shot `pm install` of the pinned Roblox
splits at first boot. On failure — bad APKs, a package-manager error, an
install timeout on a wedged session, or APKs missing from the image — it
logged one WARN to the container log and wrote **no marker**. The
dashboard card then read "installing_roblox" forever with zero alert:
`df-watchdog.sh` had no stuck-install check, and the keepalive deliberately
skips uninstalled packages, so nothing ever retried or reported it.

On a fresh fleet (new image build, several instances created at once) a
bad APK bake would wedge every card silently.

## Fix
- `boot-complete.sh` writes `roblox_install_failed` (reason on the first
  line, same marker convention as `keepalive.degraded` / `.roblox_drift`)
  on each failure path, and clears it whenever the client is present —
  so a later successful run (e.g. after a container restart retries the
  install) recovers cleanly.
- `df-watchdog.sh` v15, check 3c (right after the 3b keepalive mirror):
  mirrors the marker into the instance `.degraded` marker → the existing
  dashboard "needs attention" badge + a single DEGRADED log line surfaced
  in the Eventos modal. Alert-only: a failed install needs a human
  decision (fix APKs / rebuild image / recreate instance). While the
  marker is present the instance counts as unhealthy, so section-6
  auto-clear waits for the recovery.

## Tests
`test_roblox_install_guard.sh` — 32/32 hermetic:
- Part 1 (real `boot-complete.sh`, fake `waydroid`): already-installed and
  success write `roblox_done` with no marker; failure writes the marker
  with pm's first line only (second line never leaks); timeout names the
  timeout; missing APKs name the cause; a failed run followed by a
  successful run clears the marker.
- Part 2 (real `df-watchdog.sh`, fake docker/curl/systemctl): marker →
  instance `.degraded` + one DEGRADED line; rerun is silent; a
  pre-existing circuit-breaker marker is never clobbered; no marker →
  silence; removing the install marker auto-clears with the "recovered"
  line; empty marker → "unknown error" default; `keepalive.degraded`
  alone keeps its own reason (no cross-talk).

## Wiring
- apply-all preflight t108 (exact-count `PASS=32 FAIL=0`).
- No new stage files: `df-watchdog.sh` already staged (→
  `/usr/local/bin/df-watchdog.sh`); `boot-complete.sh` ships in the image
  via Dockerfile COPY (next `waydroid-instance` rebuild).
