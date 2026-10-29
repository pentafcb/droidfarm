# log-trimmer — DEPLOY

## What it is
df-watchdog.sh **section 10** (v10): once per minute-cron run, any Docker
container `*-json.log` over 1GB is trimmed in place to its last 1GB
(copytruncate — same inode, dockerd's open fd stays valid). This is the
durable fix for the 69G of runaway container logs observed 2026-10-08,
which starved the disk and plausibly prevented new Android boots. The v6
disk auto-recovery can't touch container logs (only dangling images +
build cache); this section closes that gap.

Design doc + failure analysis: `pending-deploy/log-trimmer/README.md`.
Original drop-in snippet: `pending-deploy/log-trimmer/section10-snippet.sh`
(now integrated verbatim as section 10 of `waydroid-image/df-watchdog.sh`;
the snippet file is kept for reference only).

## Deploy
Rides the apply-all queue from HEAD — no new stage files
(`waydroid-image/df-watchdog.sh` is already in the stage list; the
minute-cron picks it up with no service restart).

Standalone (equivalent, if you ever need it without a full deploy):
```
scp ~/workspace/droidfarm/waydroid-image/df-watchdog.sh root@209.222.10.93:/usr/local/bin/df-watchdog.sh
ssh root@209.222.10.93 "bash -n /usr/local/bin/df-watchdog.sh"
```
No restart needed — the minute-cron picks it up.

## Verify (on the VPS)
1. `bash -n /usr/local/bin/df-watchdog.sh` — clean.
2. Wait ~2 min, then `tail -5 /var/log/df-watchdog.log` — no errors;
   if any `*-json.log` was over 1GB, a `log-trim: trimmed ...` line.
3. `ls -la /var/lib/docker/containers/*/*-json.log` — no file over 1GB
   (already-trimmed ones stay small until they regrow).

## Safety properties (all covered by test_log_trimmer.sh)
- In-place trim: same inode, logging continues, sparse holes free the disk.
- Only `*-json.log` files; instance data volumes are never touched.
- Last 1GB always kept for failed-boot post-mortems (Penta's 2026-10-09
  "keep last 1GB" instruction; the minute-cron enforces it continuously).
- Self-limiting: a trimmed log is skipped until it regrows past 1GB —
  no storm budget needed.
- Failure (tail/cat error) logs one WARN and moves on; never raises.

## Relation to utils/trim-docker-logs.sh
That standalone hourly-cron script (Penta's request, installed live
2026-10-09 01:53 UTC) uses the same 1GB/1GB thresholds. With section 10
deployed the minute-cron enforces the same rule continuously, so the hourly
job becomes redundant on the main VPS — but it is harmless (both are
in-place/fd-safe with identical thresholds) and stays as the fallback for
a box without the watchdog.

## Tests
`pending-deploy/log-trimmer/test_log_trimmer.sh` — 23/23 hermetic checks:
extracts section 10 verbatim from the real df-watchdog.sh and runs it
against fake log dirs (in-place trim, tail-content, under-threshold skip,
no-match tolerance, defaults, failure WARN, mixed files, scope guard,
bash -n). Wired into deploy-all.sh preflight.
