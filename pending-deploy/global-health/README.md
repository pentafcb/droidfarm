# global-health — global watchdog markers → dashboard banner

## Gap

The watchdog's circuit breaker trips farm-wide markers when a global
budget is exhausted — manager service restarts, dockerd restarts, disk
auto-prune, host memory pressure — and auto-recovery pauses. But only
per-instance markers had a dashboard surface (card badges). A tripped
global marker showed up solely as one line in the Eventos log; after the
log scrolled, the farm looked healthy while auto-recovery was paused.

## Fix

`global_watchdog_health()` in the manager reads the four global marker
files, `GET /api/instances` carries them as `watchdog_global`, and the
dashboard renders a red "Atención del sistema" banner above the grid
(Spanish labels, watchdog reasons in the tooltip) that auto-clears when
the watchdog clears the markers.

## Files changed

- `manager/app.py` (canonical): `GLOBAL_WATCHDOG_MARKERS` +
  `global_watchdog_health()` + one field in the GET /api/instances
  response.
- `manager/static/index.html` (canonical): `#globalBanner` element +
  `updateGlobalBanner()` block + one call in `refresh()`.

## Tests

`test_global_health.py` — 43/43 hermetic checks (see DEPLOY.md).

## Deploy

Rides apply-all from HEAD (preflight t132). No new stage files.
