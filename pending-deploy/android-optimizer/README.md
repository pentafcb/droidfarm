# android-optimizer

One canonical Android optimizer for every Waydroid instance, applied two ways:

- **At boot** — `entrypoint.sh` installs `waydroid-image/android-optimize.sh`
  (from `/opt/android-optimize.sh` in the image) into the instance's shared
  `jerusalem/` dir and runs it inside Android. Every container start
  re-applies it, so runtime drift (Doze re-enabling, caches refilling)
  self-corrects.
- **Live** — `push-optimizer.sh` applies it to already-running containers
  without restarting them (also a `finish-deploy.sh` step).

The command set is the verified working set only (`pm`, `svc`, `dumpsys`).
`settings put` is broken in this Waydroid build (AppOps NPE) and
`bg-dexopt-job` is SecurityException-blocked — both are deliberately absent,
so the script can never fail halfway on a known-broken command.

Tests: `test_android_optimizer.sh` (hermetic — fake docker/volumes/waydroid).
