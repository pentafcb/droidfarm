# node-offline-badge

A remote instance on a wing node that stopped heartbeating contributes
nothing to `fetch_remote_statuses()`, so its card reads "stopped" —
exactly like an instance the user deliberately stopped. Only the Nodes
modal's online flag said which one it was, one modal deep. Same
visibility class as nodes-attention-badge (degraded wing nodes) and
global-health (main-node markers), but for node reachability itself.

## What it does

- `manager/app.py` gains `node_offline(providers, prov)`:
  True when the provider record is missing (node deleted, instances
  lingering) or its `last_seen` is stale (>120s — the same staleness
  rule as `nodes_attention_summary()` and the `/api/providers`
  "online" flag) or unparseable. Never raises; fail-closed toward
  showing the badge, since a corrupt record means reachability cannot
  be confirmed.
- GET `/api/instances` carries `node_offline` per instance: the remote
  branch via the helper, the local branch as `False` (the manager
  answering the poll IS the local node). No status semantics change:
  the card still reads "stopped" — the badge adds the reason, it does
  not re-litigate the status, so batch actions and other status
  consumers are untouched.
- Dashboard: a red "Nodo sin conexión" badge (reuses the
  `.setup.degraded` class, no new CSS) on the card, with the node name
  in the tooltip (escaped via `esc()`). It outranks the degraded and
  drift badges: both come from the last-known-good cache and are stale
  once the node is gone. The Ver button is disabled while offline (a
  viewer on a dead node can only fail); Start/Stop keep their existing
  server-side 409 "node offline" guard.

## Deliberate boundary

This badge is about heartbeat staleness only. A heartbeating node whose
`/api/agent/instances` fetch fails still serves its 30s last-known-good
cache, then reads "stopped" without the badge — that case is transient
by construction (30s window) and is not conflated with a dead node.

## Tests

`test_node_offline_badge.py` — 48 checks, hermetic (real app import,
faked provider dicts; dashboard contract + node driver over the
extracted badge block): staleness boundary (119s/119.99s/121s, future,
numeric-string tolerated), missing/garbage records, both API branches
emit exactly two `node_offline` sites, badge priority over degraded,
Ver disabled, tooltip escaping, no emojis, no innerHTML.

## Preflight

t135, exact-count grep `PASS=48 FAIL=0`.
