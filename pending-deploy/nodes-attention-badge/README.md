# nodes-attention-badge

Remote-node selfwatch DEGRADED maps already surface inside the Nodes
modal (per-node "needs attention" badge), but the topbar Nodos button
itself gave no signal — a degraded wing node was invisible until Penta
opened the modal. Same visibility class as global-health (main-node
markers -> banner), but for wing nodes.

## What it does

- `manager/app.py` gains `nodes_attention_summary(providers=None)`:
  iterates the providers dict (wing nodes only — the local node is a
  pseudo-provider added only to the `/api/providers` response, never
  here), keeps nodes seen within 120s (same staleness rule as the
  Eventos merge and the `/api/providers` "online" flag, so a dead node's
  last-known degraded map can't pin the badge forever) whose
  `selfwatch.degraded` map is a non-empty dict. Returns
  `[{node, markers, reason}]`; never raises; names truncated to 40,
  marker keys to 32, reasons to 160.
- GET `/api/instances` carries `nodes_attention` alongside
  `watchdog_global` (same response the grid refresh already fetches
  every 10s — no new fetch, no heartbeat change).
- Dashboard: a red count badge (`#nodesBadge`, `.nodebadge` CSS) inside
  the Nodos button; `updateNodesBadge()` renders the count, node names
  + markers + reasons in the tooltip, textContent-only; an empty list
  hides the badge again (markers auto-clear when the node recovers).
  Clicking the button still opens the Nodes modal (handler untouched).

## Tests

`test_nodes_attention_badge.py` — 58/58 hermetic checks: summary shapes
(fresh/stale/boundary/garbage/missing, degraded shape variants,
truncation, multi-node, no-arg load_providers path), `/api/instances`
contract, agent cross-check (`selfwatch_state()` really builds the
degraded map the heartbeat handler consumes), dashboard contract +
behavioral node test of the extracted badge block.

## Deploy

Rides apply-all from HEAD (`manager/app.py` + `manager/static/index.html`
already in the stage list). No new stage files, no version bump, no
manager/dashboard changes beyond the badge itself.

Verify after deploy: degrade a wing node (or fake its heartbeat
degraded map) -> Nodos button shows a red count badge; tooltip names
the node + marker + reason; recovering the node hides the badge on the
next grid refresh.
