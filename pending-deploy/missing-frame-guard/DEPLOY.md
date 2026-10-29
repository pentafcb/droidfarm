# DEPLOY: missing-frame-guard

## What

Both stale-stream repairs only handled a STALE `live.png`. When the
in-container screencap loop was alive but `screencap` failed every
cycle, `live.png` was never written at all — and the missing-file case
was never repaired: the main-node watchdog's section 4 only ran inside
`if [ -f live.png ]`, and the wing agent's selfwatch check 3 computed
`age = -1` on the missing file, which fails its `age >= 30` gate. The
viewer then spun on MJPEG reconnects forever with no watchdog repair
and no DEGRADED badge, even though the loop process was alive.

Now (both sides, same semantics): missing `live.png` + booted marker +
screencap loop alive + script present -> pkill + loop restart under
the SHARED staleloop budget (3600s/10), log
"screencap missing (no live.png)", recoveries bumped. Booted gate: a
first-boot instance legitimately has no frame yet. A dead loop is the
helper check's job (and a loop restarted by the helper check this
pass/cycle is skipped via flag, so one pass never restarts twice);
a timed-out pgrep is UNKNOWN, never a restart. Missing script ->
per-day WARN, no budget burn (same as the stale branch). Budget trip
-> DEGRADED marker, auto-recovery pauses (existing dashboard badge).

Files changed: `waydroid-image/df-watchdog.sh` (section 3 flag +
section 4b), `wing/agent.py` (1.0.41 -> 1.0.42, check 2 flag +
check 3 missing branch). No manager/dashboard change: the existing
DEGRADED badge surfaces the trip, and no wire protocol changed.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`df-watchdog.sh` and `wing/agent.py`; no new stage files. The wing
agent self-updates to 1.0.42 on the next heartbeat after the manager
deploy; the vultr node additionally needs Penta's installer re-run
only if its agent predates self-update (same re-run already needed
for earlier agent versions).

## Standalone (if apply-all is unavailable)

```sh
scp waydroid-image/df-watchdog.sh root@209.222.10.93:/usr/local/bin/df-watchdog.sh
scp wing/agent.py root@209.222.10.93:/root/jerusalem/agent.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

No service restart is needed for the watchdog alone (minute-cron
picks it up); the manager restart is for the agent file.

## Verify

1. `systemctl is-active jerusalem` -> active.
2. Dashboard root returns HTTP 200.
3. On a booted instance whose loop is alive but `live.png` is absent
   (or deleted for the test), the watchdog/agent restarts the loop
   within one minute-cycle and the frame returns; the watchdog log
   shows "screencap missing (no live.png) -> restart loop".
4. A first-boot instance with no frame yet is NOT restarted for
   the missing file (booted gate).
5. Existing behavior unchanged: `pending-deploy/remote-selfwatch/
   test_agent_selfwatch.py` still passes (its missing-png assertion
   was updated to the new contract), stale-stream restarts unchanged.
