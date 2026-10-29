# DEPLOY — df-watchdog v7 (memory-pressure watch + OOM-killer surfacing)

## What it is
`waydroid-image/df-watchdog.sh` v7 adds a memory-pressure watch (section 9) on
top of v6. The 31GB host now runs 20×2GB-limited instances (40GB of limits, ~1GB
real use each) — an OOM storm would kill containers en masse, and nothing
watched for it.

- **Memory pressure (alert-only, never kills)**: `MemAvailable` <1GB on three
  consecutive minute-checks → two `WARN memory pressure` lines, then a
  `/tmp/df-watchdog/mem.degraded` marker (reason on first line: host memory
  low, auto-recovery paused where it touches memory) with budget semantics
  (>3 low-memory hours… see script). Relief (≥1GB available) clears the marker
  and logs "memory pressure relieved". Threshold overridable via env for tests
  (`DF_PROC_MEMINFO`).
- **OOM-killer surfacing**: fresh `Out of memory` lines in dmesg → one loud
  WARN naming the killed process, deduped by a cursor (`DF_DMESG` overridable),
  so a repeat doesn't spam.

Nothing is ever stopped, killed, or restarted by this section — containers,
helpers, and the manager are untouched. It is purely early warning.

## Tests
`test_watchdog_v7.sh` — 11/11 pass (verbatim section 9 extracted from the
real script, fake meminfo/dmesg, hermetic): healthy silent + stale marker
cleared, low-mem 3x → WARN,WARN,DEGRADED with reason on marker, relief clears,
OOM lines surfaced once + deduped, section never contains kill/stop/rm.

## Deploy (rides apply-all — no new stage files)
`df-watchdog.sh` is already in the apply-all stage list; v7 deploys from repo
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
- `grep -n "memory pressure" /usr/local/bin/df-watchdog.sh` → section 9 present.
- Wait ~2 cron cycles, then `tail /var/log/df-watchdog.log` — expect normal
  lines, no `WARN memory pressure`/`mem.degraded` unless the host is actually
  under 1GB available.
- OOM path: `dmesg | grep -i "out of memory"` — if empty, nothing to verify;
  a real OOM kill now produces one WARN line in the watchdog log.

## Rollback
Restore `/usr/local/bin/df-watchdog.sh` from the timestamped backup the
apply-all run leaves next to it (or re-scp the previous version); no restart
needed.
