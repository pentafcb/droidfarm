# global-health — global watchdog markers → dashboard banner

## What it is

Per-instance watchdog DEGRADED markers already surface as dashboard card
badges (instance-health), but the watchdog's GLOBAL markers — manager,
docker, disk, mem budgets tripped, farm-wide auto-recovery paused — were
only transient Eventos log lines. Once the log scrolled past, the
dashboard kept showing green cards while the farm was paused.

- `manager/app.py`: `global_watchdog_health()` reads the four global
  markers from `DF_ST` (`/tmp/df-watchdog`): `manager.degraded`,
  `docker.degraded`, `disk.degraded`, `mem.degraded` (first line = reason,
  200-char cap, generic fallback; best-effort, never raises). GET
  `/api/instances` now carries `watchdog_global` (list of `{key, reason}`,
  `[]` when all green).
- `manager/static/index.html`: `#globalBanner` above the grid (reuses the
  red `.connbanner` style) + `updateGlobalBanner()` in the refresh loop:
  Spanish subsystem labels, reasons in the tooltip, textContent-only.
  An empty list hides the banner again (markers auto-clear when the
  watchdog sees all green). No new stage files — both files already ride
  apply-all from HEAD.

## Tests

`test_global_health.py` — 43/43 hermetic checks: marker parsing (all
four, order, empty/multiline/truncation, directory-as-marker, missing
dir, st_dir override), /api/instances contract, watchdog cross-check
(the script really writes all four markers), dashboard contract +
behavioral node test of the extracted banner block.

## Deploy

Rides apply-all from HEAD (preflight t132). No service restart strictly
required for the dashboard file (served from disk), but apply-all
restarts the manager once for the app.py change anyway.

## Verify after deploy

1. Dashboard loads; with no tripped markers, no banner above the grid.
2. `touch /tmp/df-watchdog/disk.degraded` on the VPS (then remove it):
   next 10s poll shows "Atención del sistema: Disco — recuperación
   automática pausada"; removing the file hides it on the next poll.
   (Test marker only — never leave a fake marker behind: the watchdog
   would otherwise report a paused state that is not real.)
3. `GET /api/instances` (authed) contains `"watchdog_global": []` when green.
