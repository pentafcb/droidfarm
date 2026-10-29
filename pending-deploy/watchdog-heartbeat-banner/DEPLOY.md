# watchdog-heartbeat-banner — DEPLOY.md

## What it is
The watchdog minute-cron touches `$ST/heartbeat` on every executed run
(after the lock is held), but the dashboard system banner only rendered
marker files (manager/docker/disk/memory/boot). A dead cron — crontab
wiped, cron daemon down, script broken before the lock — means NO
auto-recovery at all, yet the dashboard showed green cards: markers
report trips, never the absence of the watcher. Only `verify-live.sh`
(run from the repo machine) would notice.

## Deploy
Rides apply-all from HEAD — no new stage files (`manager/app.py` +
`manager/static/index.html` already staged). No watchdog script change
(it already touches the heartbeat), no agent change, no VERSION bump:
surfacing-only.

1. From an SSH-capable session:
   `bash pending-deploy/apply-all/deploy-all.sh` (or `sync-newbox.sh --yes`
   for the new box).

## Verify
- On the VPS: `stat -c %Y /tmp/df-watchdog/heartbeat` refreshes every
  minute; the dashboard system banner stays hidden while it does.
- Kill the cron (test only): `rm /tmp/df-watchdog/heartbeat` (or age it
  past 600s) → within one dashboard refresh the red "Atención del
  sistema" banner shows a "Watchdog" entry; touch a fresh heartbeat →
  the entry clears on the next refresh.
- Wing nodes need no equivalent: a dead agent stops heartbeating and the
  node-offline badge already covers it.

## Tests
- `python3 pending-deploy/watchdog-heartbeat-banner/test_watchdog_heartbeat_banner.py`
  (18/18: fresh/missing/stale/599-vs-601 boundary heartbeats,
  marker+watchdog ordering, missing dir, heartbeat-as-dir, DF_ST
  default, dashboard label, watchdog touch contract, wiring).
- Preflight t138 (`PASS=18 FAIL=0`).
- Regressions: global-health suite (t132, 44/44) and boot-recovery-badge
  suite (t137, 42/42) fixtures now touch a fresh heartbeat so their
  exact-output marker assertions model a firing watchdog; no check
  counts changed in either suite.
