# Log trimmer — integrated as watchdog section 10 (2026-10-09)

**Status:** the snippet below is now integrated verbatim as **section 10**
of `waydroid-image/df-watchdog.sh` (v10), with `DF_LOG_GLOB`,
`DF_LOG_TRIM_MAX`, `DF_LOG_TRIM_KEEP` test overrides. It rides the
apply-all queue from HEAD (`df-watchdog.sh` is already staged; the
minute-cron picks it up, no restart). Tests:
`pending-deploy/log-trimmer/test_log_trimmer.sh` (23/23 hermetic),
wired into the deploy-all.sh preflight. Deploy doc:
`pending-deploy/log-trimmer/DEPLOY.md`.

## What the 69G logs actually are
Docker captures every container's stdout/stderr into
`/var/lib/docker/containers/<id>/<id>-json.log` and never rotates it by
default. In `waydroid-image/entrypoint.sh`, `waydroid container start` and
`waydroid session start` run backgrounded and **inherit the container's
stdout**, so the full Waydroid/LXC/Android session chatter (boot logs,
logcat-level noise, binder/LXC messages) floods the Docker log. The tagged
`[entrypoint]` lines are a tiny fraction.

**What's NOT in them:** instance data, Roblox accounts/passwords
(`roblox_accounts.json` is a separate file), or anything irreplaceable.
Mostly diagnostic noise. One caveat: after a failed boot, the log tail is
the only post-mortem — the trimmer always keeps the last 1GB (Penta's instruction), so that
survives.

## The patch
`section10-snippet.sh` — append to the end of `df-watchdog.sh` (before
`exit 0`). Once per minute-cron run it trims any `*-json.log` over 1GB
down to its last 1GB (thresholds follow Penta's 2026-10-09 instruction), **in place** (`cat tmp > log` keeps the same
inode), so dockerd's open fd stays valid and logging continues. Writes
past the new EOF are sparse holes — disk usage really drops. Reversible,
no Docker restart, no instance bounces.

The watchdog runs on the VPS via minute-cron, so this takes effect
without touching containers.

## Deploy
Rides the apply-all queue (`bash pending-deploy/apply-all/deploy-all.sh`
from an SSH-capable session) — `waydroid-image/df-watchdog.sh` is already
a stage file, so section 10 deploys with the queue and the minute-cron
picks it up (no service restart). Standalone equivalent:
`scp waydroid-image/df-watchdog.sh root@209.222.10.93:/usr/local/bin/df-watchdog.sh`
+ `bash -n` check. Details: `pending-deploy/log-trimmer/DEPLOY.md`.

## Relation to the boot failure
The user reported (2026-10-08T23:20:17Z) that no Android instance has
booted in hours. A disk starved by 69G of logs is the prime suspect —
Waydroid init / image writes can fail on a full disk, and the watchdog's
v6 disk auto-recovery only prunes dangling images + build cache (it can't
touch container logs). The 22:46Z manual clearing freed the space; if the
logs are already growing back, the trimmer is the durable fix. Unverified
from here (workers lack VPS SSH) — the delivering agent should confirm
with `df -h /var/lib/docker` and `docker ps` / container states, then
decide whether a Docker restart + daemon log rotation (the permanent fix
the user was offered) is worth the instance bounce.
