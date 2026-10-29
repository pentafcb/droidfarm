# boot-recovery-badge — boot-recovery pause surfacing

## The gap

The host-boot fleet recovery (main-node `df-watchdog.sh` section 0,
wing agent `sw_boot_recovery`) pauses the whole fleet start when the
binder pool is blocked or incomplete: a non-empty docker bind-stub
directory, binderfs unavailable, or a short pool. Until now that pause
was log-only — one ALERT/WARN line in the watchdog log. The dashboard
grid kept showing plain "stopped" cards while nothing could start, and
the Eventos log scrolled past it. The other farm-wide pauses
(manager/docker/disk/memory budgets) already surface as the red
"Atención del sistema" banner (global-health); boot recovery was the
fifth global state with no banner entry, and the wing node's equivalent
never reached the Nodes "needs attention" badge.

## Changes

- `waydroid-image/df-watchdog.sh` (v16, section 0): the paused paths
  write `$ST/boot.degraded` (reason on the first line, same convention
  as the other global markers) — stub-blocked and pool-incomplete.
  The marker clears as soon as the pool is complete again (the pause
  is over even while the stagger is still draining), when the queue
  drains, and when the boot window is missed. It never outlives the
  pause it describes.
- `manager/app.py`: `GLOBAL_WATCHDOG_MARKERS` gains
  `("boot", "boot.degraded", "boot recovery paused")`, so
  `GET /api/instances` carries it in `watchdog_global` and the
  dashboard banner renders it. `_WATCHDOG_GLOBAL_FILES` gains
  `boot.degraded`, so an instance literally named "boot" can never
  sweep the farm-wide marker on delete.
- `manager/static/index.html`: `GLOBAL_HEALTH_LABELS` gains
  `boot:'Arranque'`.
- `wing/agent.py`: `sw_boot_recovery()` writes `st_dir/boot.degraded`
  on the pool-paused and incomplete paths and clears it on
  pool-complete, queue-drained, window-missed, and sentinel-exists
  paths. No manager/dashboard change needed: `selfwatch_state()`
  already maps every `*.degraded` file into the heartbeat degraded
  map, so it surfaces in the Nodes "needs attention" badge and the
  Nodos-button badge. `_SELFWATCH_GLOBAL_FILES` gains `boot.degraded`
  for the same "boot"-named-instance guard.
- No agent VERSION bump: this writes a marker file only — no new
  recovery action, no protocol change, the manager's auto-login gate
  is untouched, and the self-update path is sha-driven. The suite pins
  VERSION at 1.0.39 so the next bump moves it deliberately, per the
  bump convention.

## Deliberate boundaries

- A one-cycle pool blip shows the banner/badge briefly until the next
  cycle's pool-complete path clears it — same as the Eventos ALERT it
  already logged; the marker is the pause state, not a latched alarm.
- When dockerd itself is down, boot recovery is skipped entirely
  (daemon guard) and `docker.degraded` is the marker that shows — the
  boot marker only covers the pool-gate pause, not the daemon outage.
- The marker is never written for a missed boot window or an
  unreadable uptime clock (fail-open paths keep today's behavior).

## Tests

`test_boot_recovery_badge.py` — 42/42 hermetic: manager marker
parsing + five-marker ordering, source contracts, both delete sweeps
keeping the global marker, real agent `sw_boot_recovery()` branches
(stub/incomplete/complete/window/sentinel), and real `df-watchdog.sh`
runs with fake docker/curl (incomplete pool, stub-blocked pool,
healthy pool clearing a stale marker).

## Deploy

Rides apply-all from HEAD (t137). See DEPLOY.md.
