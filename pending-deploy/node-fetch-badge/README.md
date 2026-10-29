# node-fetch-badge

The mirror case the node-offline badge deliberately left out: a wing
node that KEEPS heartbeating (so `node_offline()` is False) but whose
`/api/agent/instances` endpoint keeps failing. `fetch_remote_statuses()`
serves the 30s last-known-good cache, then the node's cards read
"stopped" — exactly like instances the user deliberately stopped — with
no badge at all. That case is only "transient by construction" while
failures are transient; a wedged agent HTTP server misleads for hours.

## What changed

- `manager/app.py`:
  - `fetch_remote_statuses()` counts consecutive failed status fetches
    per provider in `_remote_fetch_failures` (success resets to 0;
    a non-200 response, a non-dict body, and a `wing_request` raise
    all count; providers skipped for stale heartbeats are never
    attempted or counted; counters for providers that no longer exist
    are pruned each call). The last-known-good cache behavior is
    unchanged — a failing node still serves cached cards for 30s.
  - `remote_fetch_degraded(providers, prov)` — True once
    `REMOTE_FETCH_DEGRADED_AFTER` (6) consecutive polls fail (~60s at
    the dashboard's 10s cadence). A missing provider record or a stale
    heartbeat returns False: those are `node_offline()`'s territory,
    and the two badges never fire together. Never raises.
  - `remote_fetch_failures(prov)` — the consecutive-failure count for
    the tooltip. Never raises; 0 when unknown.
  - GET `/api/instances` remote branch emits `node_fetch_degraded` +
    `node_fetch_failures`; the local branch emits `False`/`0` so the
    response shape stays uniform.
- `manager/static/index.html`: the card renders a red "Nodo sin
  respuesta" badge (reuses `.setup.degraded`, no new CSS; node name
  escaped, failure count in the tooltip) ranking below node_offline
  (a dead node outranks a failing endpoint) and above degraded/drift
  (both come from the last-known-good cache and are stale once fetches
  fail). The Ver button is disabled while the badge shows.

## Deliberate boundaries

- Threshold 6, not 1: a single failed poll is a blip (the 30s cache
  covers it); the badge means the endpoint is persistently failing.
- No auto-refresh trigger: mirrors the node_offline precedent — the
  badge clears on the next regular 10s poll once fetches recover.
- Ver disabled conservatively: in the common wedged-agent case the
  viewer (which also goes through the agent) can only fail; the card
  already reads "stopped", so no working viewer is hidden. If only the
  instances route were broken while streams still worked, Ver stays
  disabled until fetches recover — accepted, since the status shown
  is "stopped" either way.
- No Nodes-modal change: the card badge is the signal, same scope as
  node-offline-badge. A node whose agent HTTP is wedged but
  heartbeating still shows "online" in the Nodes modal — the card
  badge is where the misleading "stopped" lived.
- In-memory counters only: a manager restart resets them (a failing
  node re-trips after 6 fresh polls, ~60s). No state file, no
  persistence to sweep on delete — the prune handles deleted nodes.

## Tests

- `test_node_fetch_badge.py` (62/62, hermetic): real
  `fetch_remote_statuses()` against a fake `wing_request` (threshold,
  reset, exception path, stale-heartbeat skip, deleted-provider prune,
  cache reuse preserved, non-dict body counts), helper boundary +
  never-raises cases, `/api/instances` contract (both branches, exactly
  two emit sites each), dashboard contract (markers, priority chain,
  Ver disabled, no auto-refresh change, Spanish, no emojis), and a node
  driver over the extracted badge block.

Preflight t139 runs it (exact-count grep `PASS=62 FAIL=0`). Rides
apply-all from HEAD — no new staged files.
