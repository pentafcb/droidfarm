# DEPLOY — df-watchdog v6 (disk auto-recovery + Roblox version-pin guard)

## What it is
`waydroid-image/df-watchdog.sh` v6 adds two malfunction-handling gaps on top of v5:

1. **Disk auto-recovery** (was alert-only): at ≥90% on `/var/lib/docker`, runs a
   bounded SAFE prune — `docker image prune -f` (dangling images only) +
   `docker builder prune -f` (build cache only). It NEVER runs volume prune or
   `image prune -a` (instance data lives in named volumes + the instance image).
   Budgeted at >2 prunes/hour → `/tmp/df-watchdog/disk.degraded` marker,
   auto-prune pauses (alert-only) until usage drops below 90%, which auto-clears.
   Freed space is logged.
2. **Roblox version-pin guard**: every minute, each booted instance's installed
   `com.roblox.client` versionName (via `dumpsys package`) is compared to
   `PINNED_ROBLOX` (2.740.931, overridable via `DF_PINNED_ROBLOX`). Drift → one
   loud `ALERT ... DRIFT` log line per drifted version (deduped by state file),
   plus a "back on pinned" line on return. No auto-remediation — a force-updated
   client needs a human decision (standing rule: keep the client pinned).
3. **Latent bug fix** (found by the v6 tests): `note_event` declared
   `local key=$1 w=$2 f="$ST/$key.ev"` on one line — bash expands `$key` in
   `f=` to the *outer* scope, so any DIRECT `note_event` call used `$ST/.ev`
   instead of the real event file (the `cb_guard` path only worked by accident,
   inheriting `key`). `f=` is now on its own `local` line. Same fix for
   `check_one`'s `local i=$1 c="wd-$i"`.

## Tests
`test_watchdog_v6.sh` — 40/40 pass (fake docker/df/curl/systemctl, hermetic):
disk healthy/prune-relieved/prune-still-high/budget-trip/auto-clear/prune-fail,
prune never touches volumes or `image prune -a`, version match/drift/dedup/
new-version/back-to-pinned/absent/dumpsys-fail/container-down, plus v4 26/26
and v5 27/27 regressions.

## Deploy (rides apply-all — no new stage files)
`df-watchdog.sh` is already in the apply-all stage list; v6 deploys from repo
HEAD. Run from an SSH-capable session:

```bash
bash ~/workspace/droidfarm/pending-deploy/apply-all/deploy-all.sh
```

Standalone (if apply-all is unavailable):

```bash
scp ~/workspace/droidfarm/waydroid-image/df-watchdog.sh root@209.222.10.93:/usr/local/bin/df-watchdog.sh
ssh root@209.222.10.93 'bash -n /usr/local/bin/df-watchdog.sh && echo SYNTAX_OK'
# no service restart needed — the minute-cron picks it up
```

## Verify on the VPS
- `bash -n /usr/local/bin/df-watchdog.sh` → clean.
- Wait ~2 cron cycles, then `tail /var/log/df-watchdog.log` — expect normal
  lines, no `ALERT`/`DEGRADED` unless something is actually wrong.
- Disk path: `df /var/lib/docker` shows use%; only ≥90% triggers a prune line.
- Version path: inside any booted instance,
  `docker exec wd-rbx1 waydroid shell dumpsys package com.roblox.client | grep versionName`
  should show `2.740.931`; a different value produces one ALERT line in the log.

## Rollback
Restore `/usr/local/bin/df-watchdog.sh` from the timestamped backup the
apply-all run leaves next to it (or re-scp the previous version); no restart
needed.
