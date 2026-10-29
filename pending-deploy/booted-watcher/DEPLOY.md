# booted-watcher — persistent booted-marker writer

## What it fixes

`entrypoint.sh` waits at most 5 minutes for `sys.boot_completed`. Under load
(fleet recovery, many instances booting at once) Android can take longer: the
one-shot window closes with **no `booted` marker ever written**, even though
Android finishes booting minutes later.

Observed in the 2026-10-09 fleet recovery: `boot_completed=1` on rbx1–rbx4
but the dashboard showed them as "booting" — and the host watchdog's
stuck-boot check then restarts a HEALTHY container at 15 minutes (restart
loop → circuit breaker DEGRADED → auto-recovery pauses).

`waydroid-image/booted-watcher.sh` is a tiny background loop (started by the
entrypoint after the one-shot wait) that writes the `booted` marker whenever
`sys.boot_completed=1` is observed and the marker is missing, and backfills
`roblox_done` when the client is present. It never deletes markers and never
restarts anything — pure surfacing. Steady-state cost is two file stats per
minute; it only probes `waydroid shell` while the marker is missing.

## Files

| Repo path | Deployed to |
|---|---|
| `waydroid-image/booted-watcher.sh` | `/root/jerusalem/booted-watcher.sh` (via apply-all), then `/opt/booted-watcher.sh` inside each container (push script or image rebuild), baked at `/opt/booted-watcher.sh` by the Dockerfile |
| `waydroid-image/entrypoint.sh` | baked into `waydroid-instance:latest` on next image rebuild (starts the watcher) |
| `waydroid-image/Dockerfile` | `COPY booted-watcher.sh /opt/booted-watcher.sh` |
| `pending-deploy/booted-watcher/push-booted-watcher.sh` | run manually on the VPS (below) |

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (stages
`booted-watcher.sh` → `/root/jerusalem/booted-watcher.sh`; no service
restart needed for the staged file itself).

After apply-all, on the MAIN VPS as root, push the watcher into the live
containers (no image rebuild needed for existing instances):

```bash
bash /root/jerusalem-deploy/push-booted-watcher.sh /root/jerusalem/booted-watcher.sh
```

The push script is idempotent (re-running replaces the script and restarts
the loop). Verify: `docker exec wd-rbx1 pgrep -f booted-watcher.sh` returns a
PID on each running instance.

For NEW instances, rebuild the image on the VPS so the entrypoint starts the
watcher automatically:

```bash
cd /root/jerusalem/waydroid-image   # or wherever the image sources live
docker build -t waydroid-instance:latest .
```

## Verify

- `bash -n` clean on all three shell files (covered by apply-all preflight).
- Test suite: `bash pending-deploy/booted-watcher/test_booted_watcher.sh`
  (hermetic, fake `waydroid`).
- On the VPS: `docker exec <c> pgrep -f booted-watcher.sh` → PID.
- Slow-boot simulation is covered by the hermetic tests (marker written when
  `getprop` flips to 1 after the "window" is missed).

## Notes

- Wing (vultr) node: its containers run the same image. Either rebuild the
  image there or run `push-booted-watcher.sh` on that node — same re-run
  Penta already owes for agent 1.0.2/1.0.3/1.0.4.
- The host watchdog needs no changes: once the marker lands, the stuck-boot
  check is satisfied and helper/stream checks proceed as before.
