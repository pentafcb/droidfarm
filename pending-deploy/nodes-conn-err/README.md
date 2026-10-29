# nodes-conn-err — Nodes modal connectivity error line

## Problem

`refreshProviders()` fetches `/api/providers` to render the Nodes modal and
its catch was a bare `catch(e){}`. When the tunnel dropped or the manager
wedged, the modal opened on an empty (or stale, after a delete) node list
with no indication anything was wrong. The grid had already solved the same
class per-viewer (stream-freshness) and for the grid itself (conn-banner);
the Nodes modal was the remaining silent surface.

## What it does

- `<p class="hint" id="provConnErr" style="display:none;color:var(--bad)">`
  right after `#provList` in the modal markup.
- `provConnErrShow()` / `provConnErrHide()` helpers (null-guarded, never
  throw): the `refreshProviders()` catch now shows the error line with
  "Sin respuesta del servidor — no se pudieron cargar los nodos."; the
  success path hides it again.
- Re-opening the modal retries the fetch automatically (the fetch runs on
  every open + after node deletes).
- Message set via `textContent` only; Spanish; no emojis.

## Files

- `manager/static/index.html` — the error element, the marked
  `// == nodes-conn-err begin ==` helper block, and the two
  `refreshProviders()` wiring lines.
- `test_nodes_conn_err.py` — 25/25 hermetic: the marked block is extracted
  and driven in node (stubbed DOM) for the hidden→shown→hidden state
  machine + exact text + idempotency + missing-element tolerance, plus
  dashboard contract checks.

## Deploy

Rides apply-all from HEAD (`index.html` already in the stage list; no new
stage files). Verify steps in DEPLOY.md.
