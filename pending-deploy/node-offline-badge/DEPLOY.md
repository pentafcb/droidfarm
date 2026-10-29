# DEPLOY: node-offline-badge

## What

Card-level "Nodo sin conexión" badge for remote instances whose wing
node stopped heartbeating (see README.md).

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
   `nodeOfflineBadge`.
3. Sign in, open the dashboard: with all wing nodes heartbeating, no
   card shows the badge and local cards are untouched.
4. Stop a wing node's agent (or wait out its 120s heartbeat window):
   its instances' cards show the red "Nodo sin conexión" badge
   instead of a plain "stopped" card, the Ver button is disabled,
   and the badge outranks any stale degraded/drift badge.
5. When the node heartbeats again, the badge disappears on the next
   grid refresh (10s poll) and the cards show live state again.
