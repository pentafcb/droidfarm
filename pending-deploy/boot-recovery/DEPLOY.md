# boot-recovery — DEPLOY

## What it is
`df-watchdog.sh` v14, new **section 0: host-boot fleet recovery**. After a
host reboot every `wd-*` container is stopped (`docker run` sets no
`--restart` policy) and the binderfs pool has dropped back to the stock
set — only the CREATE path (`manager ensure_binder_devices`) ever
restored it. Without this section the minute-cron's dead-container check
fired for EVERY stopped instance in the same pass: a 20-way Waydroid boot
storm on the 8-vCPU box, and `docker start` on containers whose
bind-mounted `/dev/binderfs/binderN` sources had vanished (docker
auto-creates empty directories at missing bind sources, so Waydroid
booted with dead binder mounts, wedged at stuck-boot, and burned the
6/hour circuit budget into mass DEGRADED).

Section 0 runs before the per-instance loop. Fresh-boot detection: the
`$ST/boot.done` sentinel is missing (`$ST` is `/tmp`, wiped by reboot)
AND host uptime is under `BOOT_WINDOW_S` (default 900s). It then:

1. **Restores the binderfs pool** — mounts binderfs if needed
   (`modprobe binder_linux` best-effort first), then writes
   `binder`..`binder63` via `binder-control` (same technique as the
   manager). Empty docker bind-stub directories are `rmdir`'d (`rmdir`
   removes ONLY empty dirs — it can never delete data) and recreated;
   non-empty stubs are left alone with one ALERT per boot and the fleet
   start paused (human cleanup). A rejected write leaves the pool count
   short and pauses the start for a retry next cycle.
2. **Stagger-starts the fleet** — at most `BOOT_MAX_STARTS` (default 3)
   `docker start` invocations per minute-cron cycle, sorted order,
   `.degraded` instances skipped, per-instance `BOOT_ATTEMPTS` (default 3)
   budget before DEGRADED (reason on the marker's first line → dashboard
   badge). Failures under budget stay queued for the next cycle.
3. **Writes the sentinel** when the queue drains (all started or
   degraded) — `check_one`'s container-down branch defers to boot
   recovery while it is active (v14 guard), so the stagger can't be
   raced into a herd.

Missed window (uptime ≥ 900s, e.g. the cron was down across the boot):
sentinel written once, normal per-instance checks own recovery —
today's behavior, no regression. Unreadable uptime: fail open, retry
next cycle.

## Files changed
- `waydroid-image/df-watchdog.sh` — v14 header block, `boot_recovery_active()`,
  `ensure_binder_pool()`, `boot_recovery()`, the `check_one` down-branch
  guard, and the section-0 call in the main flow. New env overrides:
  `DF_BOOT_WINDOW_S` (900), `DF_BOOT_MAX_STARTS` (3), `DF_BOOT_ATTEMPTS`
  (3), `DF_PROC_UPTIME`, `DF_BINDERFS`, `DF_BINDER_COUNT` (64); garbage
  values sanitize to defaults.
- Test-only changes: `pending-deploy/watchdog-v{4,5,6}/test_watchdog_v*.sh`
  and `pending-deploy/watchdog-timeout/test_watchdog_timeout.sh` pin a
  fake high uptime (`$T/uptime` = 5000s) so they test `check_one`'s strike
  logic, not boot recovery, on young VMs; the timeout suite's `dw`-site
  contract updated 11 → 13 (the two new sites are `dw`-bounded).

## Deploy
Rides **apply-all from HEAD** (`df-watchdog.sh` is already in the stage
list → `/usr/local/bin/df-watchdog.sh`; the minute-cron picks it up, no
service restart needed):

```bash
# from the repo machine, SSH-capable session:
bash pending-deploy/apply-all/deploy-all.sh
```

## Verify
- `bash -n` clean (preflight syntax gate covers this).
- `pending-deploy/boot-recovery/test_boot_recovery.sh` → 50 passed,
  0 failed (preflight t101).
- Regressions: watchdog-v4 26/26, v5 27/27, v6 40/40, v9 34/34,
  watchdog-timeout 50/50, watchdog-v7 11/11, v12 34/34, lock 22/22,
  keepalive-v2 38/38.
- On the VPS (after a host reboot): `grep "boot recovery" /var/log/df-watchdog.log`
  shows the pool restore + staggered starts; `ls /dev/binderfs | wc -l`
  → 65 (64 devices + binder-control); `docker ps --format '{{.Names}}'`
  fills over ~7 cycles for a 20-instance fleet, not all at once.

## Rollback
Re-run apply-all from the previous commit (single scp of
`df-watchdog.sh`; minute-cron picks it up). Deleting `$ST/boot.done`
re-arms boot recovery for the next cycle if needed.
