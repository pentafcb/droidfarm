# watchdog-liveness — DEPLOY.md

## What it is
Nothing proved the df-watchdog minute-cron is actually *firing*: the file
could be deployed, the cron daemon active, and still no run ever executing.
A healthy no-op run logs nothing, so the log mtime can't prove liveness,
and the v11 lock dir is released at exit. `df-watchdog.sh` now touches
`$ST/heartbeat` on every executed run, after the lock is held. A silent-exit
second run never touches it. `verify-live.sh --ssh` reports the heartbeat
age (computed server-side, no client clock skew): missing -> WARN "never
fired", >300s -> WARN "not executing — check /etc/cron.d/jerusalem-watchdog",
else OK.

## Deploy
Rides apply-all from HEAD — no new stage files (`waydroid-image/df-watchdog.sh`
was already staged to `/usr/local/bin/df-watchdog.sh`; `verify-live.sh`
runs from the repo machine).

1. From an SSH-capable session:
   `bash pending-deploy/apply-all/deploy-all.sh` (or `sync-newbox.sh --yes`
   for the new box).
2. From the repo machine:
   `bash pending-deploy/live-verify/verify-live.sh --ssh` — expect
   `[OK] df-watchdog firing (heartbeat Ns ago)`.

## Verify
- On the VPS: `stat -c %Y /tmp/df-watchdog/heartbeat` refreshes every minute.
- Heartbeat is an empty file (mtime only); never instance data, never secrets.

## Tests
- `bash pending-deploy/watchdog-liveness/test_watchdog_liveness.sh` (11/11:
  real watchdog, fake docker/curl/systemctl — touch/refresh/foreign-lock
  no-touch/broken-lock no-touch/DF_ST honored/healthy-run no-op).
- `bash pending-deploy/live-verify/test_live_verify.sh` (119/119 incl.
  T39 fresh -> OK, T40 missing -> WARN, T41 stale -> WARN, T42 server-side
  age regression guard).
- Preflight t113 (`PASS=11 FAIL=0`) + t32 (`PASS=119 FAIL=0`).
