# conn-banner — dashboard grid connectivity banner

## Problem

`refresh()` polls `/api/instances` every 10s and its catch was a bare
`/* keep old */` — when the tunnel dropped or the manager wedged, the user
kept staring at stale cards with no indication anything was wrong. (The
stream-freshness work had already solved this *per viewer*; the grid had
no equivalent.)

## What it does

- `CONN_BANNER_AFTER = 2` consecutive failed grid polls → red banner above
  the grid: "Sin respuesta del servidor — reintentando… (N)".
- One successful poll → banner hides, counter resets.
- Cards keep old data on failure (unchanged).
- 401 skips the counter (logout() reloads the page on 401 anyway).
- Banner text set via `textContent` only; Spanish; no emojis.

## Files

- `manager/static/index.html` — `.connbanner` CSS, the banner element
  (between fleetbar and grid), the `// == grid-conn-banner begin ==` block,
  and the two `refresh()` wiring lines.
- `test_conn_banner.py` — 29/29 hermetic: the marked block is extracted
  and driven in node (stubbed DOM) for the hidden→shown→hidden state
  machine, plus dashboard contract checks.

## Deploy

Rides apply-all from HEAD (`index.html` already staged); preflight t56.
Standalone: scp the file (served from disk, no restart). See DEPLOY.md.
