# df-watchdog v15 — docker-daemon guard

## Problem

The watchdog watched containers, helpers, streams, disk, memory, and the
manager — but NOT the docker daemon itself. A down daemon looked exactly
like 20 dead containers: `docker ps` fails fast (daemon connection
refused, not a timeout), so section 1's container-down branch fired for
every instance — 2 strikes → `docker start`, which also fails — and
within the hour the 6/hour circuit breaker marked EVERY instance
DEGRADED and paused auto-recovery farm-wide. The root cause (dead
dockerd) was never repaired, the markers lied about the cause, and even
after a human restarted dockerd nothing recovered (section-6 auto-clear
needs all-green checks, which down containers never give).

## Fix (waydroid-image/df-watchdog.sh, section 0b)

- New daemon probe before ANY docker-dependent recovery: `dw $DW_FAST
  info` (timeout-bounded; a wedged daemon fails after DW_FAST instead of
  hanging the run). A timeout (124) counts as down, not unknown —
  instance recovery cannot run against a daemon that will not answer.
- While the probe fails: boot recovery, the per-instance loop, and the
  disk auto-prune are skipped (no strikes burned, no budgets tripped, no
  misleading markers). One log line per cycle.
- After 2 consecutive failed probes: `systemctl restart docker`
  (2/hour `cb_guard` budget, same shape as the v5 manager self-check).
  Beyond budget → global `docker.degraded` marker (reason on first line),
  alert-only until a healthy probe auto-clears it.
- A dockerd restart is recoverable by design: `docker run` sets no
  `--restart` policy, so stopped containers come back through the normal
  section-1 start path once the probe passes — with clean budgets,
  because the outage burned none.
- Disk/memory/manager/log sections are docker-independent and keep
  running during an outage (the disk alert WARN still fires; only the
  prune attempt is skipped).

## Tests

`test_watchdog_v15.sh` — 48/48 hermetic (real watchdog vs fake
docker/curl/systemctl/df). See DEPLOY.md for the case list.
Preflight t130 (`PASS=48 FAIL=0`).

## Follow-up

Wing agent selfwatch mirror (same flaw on remote nodes) — next item.
