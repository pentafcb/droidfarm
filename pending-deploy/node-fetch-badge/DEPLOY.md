# DEPLOY: node-fetch-badge

## What

Card-level "Nodo sin respuesta" badge for remote instances whose wing
node keeps heartbeating but whose `/api/agent/instances` endpoint keeps
failing (see README.md). This closes the deliberate boundary the
node-offline-badge package left open: that badge covers heartbeat
staleness only, so a heartbeating node with a failing status endpoint
read "stopped" with no badge at all once the 30s last-known-good cache
expired.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/app.py` and `manager/static/index.html`; no new stage files.

## Standalone (if apply-all is unavailable)

```sh
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
scp manager/static/index.html root@209.222.10.93:/root/jerusalem/static/index.html
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

Note: the static file is served from disk per request, so the restart
is only needed for the `app.py` change.

## Verify

1. `systemctl is-active jerusalem` -> active.
2. Dashboard root returns HTTP 200; served `index.html` contains
   `nodeFetchBadge`.
3. Sign in, open the dashboard: with all wing nodes heartbeating and
   answering, no card shows the badge and local cards are untouched.
4. Break a wing node's status endpoint while heartbeats keep flowing
   (e.g. stop the agent's HTTP handler, or point the manager at a
   stub that 500s `/api/agent/instances`): after ~60s (6 failed
   polls) its instances' cards show the red "Nodo sin respuesta"
   badge (failure count in the tooltip) instead of a plain "stopped"
   card, the Ver button is disabled, and the badge outranks any stale
   degraded/drift badge but not the node-offline badge.
5. When the endpoint answers again, the badge disappears on the next
   grid refresh (10s poll) and the cards show live state again.
