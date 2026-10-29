# roblox-install-guard — DEPLOY

## What it is
Surfaces a failed one-shot Roblox install on the dashboard instead of
leaving the card on "installing_roblox" forever.

- `waydroid-image/boot-complete.sh`: writes `roblox_install_failed`
  (reason on the first line) into the instance data dir on every failure
  path — `pm install` failure (first line of pm output, 160-char cap),
  install timeout, or APKs missing from the image — and clears it whenever
  the client is observed present (fresh success or already-installed).
- `waydroid-image/df-watchdog.sh` (v15, check 3c): mirrors
  `$base/roblox_install_failed` into the instance `.degraded` marker
  (reason on first line) so the existing dashboard "needs attention" badge
  shows it; the instance counts as unhealthy until boot-complete clears its
  own marker on a later successful run (section-6 auto-clear then fires
  with the "recovered" log line). Not routed through `cb_guard` —
  surfacing, not auto-recovery: a failed install needs a human (fix the
  APKs / rebuild the image / recreate the instance). A pre-existing
  circuit-breaker marker is never clobbered.

No manager or dashboard changes: the `.degraded` marker flows through the
existing `instance_health()` → `/api/instances` → badge path, and the
Eventos modal picks up the DEGRADED log line.

## Deploy
Both files ride apply-all from HEAD — no new stage files:
- `waydroid-image/df-watchdog.sh` → `/usr/local/bin/df-watchdog.sh`
  (minute-cron picks it up, no restart).
- `waydroid-image/boot-complete.sh` ships inside the image via the
  Dockerfile `COPY` (new instances get it after the next
  `waydroid-instance` image rebuild; the morning's `vps-image-build.sh`
  covers this).

Live containers already booted keep their old boot-complete.sh — the
install already ran there (one-shot). A container whose install failed
before this change has no marker; its card still reads
"installing_roblox" until the instance is recreated from the new image
(the failure mode this guard prevents going forward).

## Verify (SSH session)
1. `bash -n` on both files (also in the apply-all syntax gate).
2. After deploy: on a test instance,
   `touch /var/lib/docker/volumes/wd-<name>-data/_data/data/media/0/jerusalem/roblox_install_failed`
   with a reason line → within a minute the dashboard card shows
   "needs attention" with "Roblox install failed: …"; remove the file →
   the badge clears with a "recovered" line in Eventos. (Do not leave the
   probe file behind.)
3. `pending-deploy/roblox-install-guard/test_roblox_install_guard.sh`
   → `PASS=32 FAIL=0`.
