# Deploy: df-watchdog v4 (restart circuit breaker)

## What changed
`waydroid-image/df-watchdog.sh` v3 -> v4 (canonical source in this repo).

The old watchdog restarted a broken instance on every 2-minute cycle forever —
a wedged container meant 30 `docker start`/`restart` per hour burning CPU on
the 8-vCPU box, and a helper script that crashes on boot got relaunched 60x/hour.

v4 counts every recovery action in a sliding window and marks the instance
DEGRADED (one log line, alert-style) when a budget is exhausted — auto-recovery
then pauses until all checks pass green, which auto-clears the flag:

- container restarts (start + stuck-boot restart share one budget): >6/hour -> DEGRADED
- per-helper restarts: >8 per 10 min -> DEGRADED
- stale screencap-loop restarts: >10/hour -> DEGRADED

Also new: test/env overrides `DF_LOG`, `DF_ST`, `DF_INSTANCES`, `DF_VOLBASE`
(harmless under the prod cron — unset there). Healthy-path behavior unchanged:
2-strike start/restart, helper + stale-stream recovery, log rotation, disk alert.

## Tests
`bash pending-deploy/watchdog-v4/test_watchdog_v4.sh` -> **26/26 pass**
(2026-10-07 ~02:10 UTC): healthy=no-op, 2-strike start/restart, storm->DEGRADED
+ no log spam, window expiry, helper crash-loop, degraded auto-clear, corrupt
event-file tolerance.

## Deploy (needs VPS SSH — blocked in worker sessions, one scp)
```bash
scp ~/workspace/droidfarm/waydroid-image/df-watchdog.sh \
    root@209.222.10.93:/usr/local/bin/df-watchdog.sh
ssh root@209.222.10.93 'cp /usr/local/bin/df-watchdog.sh /usr/local/bin/df-watchdog.sh.bak-v4-pre 2>/dev/null; bash -n /usr/local/bin/df-watchdog.sh && grep -q cb_guard /usr/local/bin/df-watchdog.sh && echo V4_PRESENT'
```

## Verify (no service restart needed — the minute-cron picks it up)
```bash
ssh root@209.222.10.93 "sleep 75; tail -5 /var/log/df-watchdog.log; ls /tmp/df-watchdog/*.degraded 2>/dev/null || echo NO_DEGRADED_MARKERS"
```
Expect: v4 runs clean (no new errors in the log), no degraded markers on a
healthy fleet, streams still fresh on the dashboard. If the local v3 on the VPS
had drifted from this repo's copy, `diff` the `.bak-v4-pre` backup before
overwriting.
