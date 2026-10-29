# DEPLOY — df-watchdog v9 (waydroid session death detection + auto-restart)

## What it is
`waydroid-image/df-watchdog.sh` v9 adds section 2b to `check_one`, closing
the last blind spot in instance malfunction handling.

**The gap**: a dead Waydroid session inside a running container was invisible
to every existing check. The container stays up (the entrypoint ends in
`tail -f`), the `booted` marker persists (so the stuck-boot check skips it),
and all three helpers (screencap loop, input daemon, Roblox keepalive) run
THROUGH `waydroid shell` — so they die too, and their watchdog restarts fail
silently. Before v9 the only symptom was a stale stream that burned the
helper and stale-loop circuit budgets until a misleading DEGRADED
("screencap-loop restarted 11 times") with the root cause never repaired:
black stream, dead input, Roblox never relaunched, until a human intervened.

**The fix**: every minute, each previously-booted instance gets a direct
session probe — `docker exec <c> waydroid shell getprop sys.boot_completed`
(the same idiom the entrypoint uses for boot detection). On death:
- `docker exec -d <c> waydroid session start` (detached, so the cron never hangs)
- the helper/stream/version checks are skipped that cycle (early return), so
  their circuit budgets are not burned by a known-dead session
- a 180s grace after each start covers the Android re-boot window (no
  double-starts, no budget burn while it comes up)
- budgeted at >4 starts/hour → instance DEGRADED, reason on the marker's
  first line → the existing dashboard "needs attention" badge shows it
  (no manager/dashboard changes needed — rides instance-health)
- recovery (session alive + all checks green) auto-clears the marker and
  resets the session budget

First boot is untouched: the probe only runs when the `booted` marker exists,
so the stuck-boot check still owns fresh instances. Nothing is ever
stopped, killed, or removed by this section — no `docker stop/kill/rm`,
no volume pruning. Instance data, credentials, and tokens are never touched.

## Tests
`test_watchdog_v9.sh` — 34/34 pass (real `log`/`note_event`/`cb_guard` +
the verbatim 2b block, fake `docker`, hermetic): alive passthrough, dead →
detached start + early return, 180s grace (no new start, no budget burn),
grace expiry → new attempt, >4/hour → DEGRADED with reason, no spam on
rerun, first-boot skip, failed start logged without grace ts, recovery
clears grace ts, section-6 auto-clear resets the session budget, probe-idiom
contract, no destructive commands.

Regression suites re-verified after the change (their fake dockers now
answer the session probe): watchdog-v4 26/26, watchdog-v5 27/27,
watchdog-v6 40/40, keepalive-v2 38/38.

## Deploy (rides apply-all — no new stage files)
`df-watchdog.sh` is already in the apply-all stage list; v9 deploys from repo
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
- `grep -n "V9-2B-START" /usr/local/bin/df-watchdog.sh` → section 2b present.
- Wait ~2 cron cycles, then `tail /var/log/df-watchdog.log` — expect normal
  lines; a dead session now produces
  `rbxN waydroid session dead -> session start (1/4 this hour)` followed by
  `rbxN waydroid session start issued`, instead of helper/stale-loop churn.
- The dashboard "needs attention" badge appears only if the session won't
  stay up (>4 starts/hour) — reason names the session.

## Rollback
Restore `/usr/local/bin/df-watchdog.sh` from the timestamped backup the
apply-all run leaves next to it (or re-scp the previous version); no restart
needed.
