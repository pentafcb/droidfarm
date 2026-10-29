# watchdog-daemon-guard — deploy

## What it is

`df-watchdog.sh` v15 (section 0b): probes the docker daemon itself
(`docker info`, dw-bounded) before ANY docker-dependent recovery.

## Why

A down docker daemon used to look exactly like dead containers:
`docker ps` fails fast (connection refused, not a timeout), so section 1's
container-down branch fired for every instance — 2 strikes, then `docker
start`, which also fails — and within the hour the 6/hour circuit breaker
marked EVERY instance DEGRADED and paused auto-recovery farm-wide. The root
cause (dead dockerd) was never repaired, the markers lied about the cause,
and even after a human restarted dockerd nothing recovered (section-6
auto-clear needs all-green checks, which down containers never give).

## Behavior

- While `docker info` fails (fast failure or dw timeout 124): boot
  recovery, the per-instance loop, and the disk auto-prune are skipped —
  no strikes burned, no budgets tripped, no misleading instance markers.
  One log line per cycle: `docker daemon unreachable (strike N/2)`.
- After 2 consecutive failed probes: `systemctl restart docker`
  (2/hour `cb_guard` budget, same shape as the v5 manager self-check;
  beyond budget → global `$ST/docker.degraded` marker, alert-only).
- On a healthy probe: strikes reset, marker + event file removed,
  `docker daemon recovered from DEGRADED` logged, normal recovery resumes.
- A dockerd restart is recoverable by design: `docker run` sets no
  `--restart` policy, so stopped containers come back through the normal
  section-1 start path once the probe passes — with clean budgets.
- Disk/memory/manager/log sections are docker-independent and keep
  running during an outage (the disk alert WARN still fires; only the
  prune attempt is skipped).

## Deploy

Rides `apply-all` from HEAD — `waydroid-image/df-watchdog.sh` is already
in the stage list. No new stage files, no service restart (the minute
cron picks the new script up).

Standalone (SSH-capable session, VPS root):

```sh
scp ~/workspace/droidfarm/waydroid-image/df-watchdog.sh root@209.222.10.93:/usr/local/bin/df-watchdog.sh
ssh root@209.222.10.93 'bash -n /usr/local/bin/df-watchdog.sh && echo SYNTAX-OK'
```

## Verify (after deploy)

- Next minute-cron cycle: `docker info` succeeds, no new
  `daemon unreachable` lines in `/var/log/df-watchdog.log`.
- Fault injection (optional, on the VPS): `systemctl stop docker` for
  3 minutes — expect `docker daemon unreachable (strike 1/2)`, then a
  `systemctl restart docker` attempt, then normal per-instance starts
  once dockerd is back. Restore with `systemctl start docker` if the
  watchdog's budget is tripped (marker auto-clears on the healthy probe).

## Tests

`test_watchdog_v15.sh` — 48/48 hermetic (real watchdog vs fake
docker/curl/systemctl/df): healthy-baseline + section-1 regression,
daemon-down skip + no budget burn, 2-strike dockerd restart, 2/hour
budget trip + alert-only + no DEGRADED spam, recovery clears the marker,
hung probe treated as down (bounded), failed restart logged, disk
prune gate (skip + alert while down, prune runs while healthy),
fresh-boot recovery skipped while down, source contracts.
Wired as preflight t130 (`PASS=48 FAIL=0`).

## Follow-up (not in this package)

The wing agent's selfwatch has the same shape of flaw (docker-down on a
remote node burns the per-instance recovery budgets); a mirror
daemon-guard in `wing/agent.py` is the next item.
