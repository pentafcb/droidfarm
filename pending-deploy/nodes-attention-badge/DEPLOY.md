# DEPLOY: nodes-attention-badge

## What

Topbar Nodos-button badge for degraded wing nodes (see README.md).

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
   `nodesBadge`.
3. Sign in, open the dashboard: with no degraded wing node, the Nodos
   button shows no badge.
4. With a wing node reporting a non-empty selfwatch degraded map
   (fresh heartbeat), the Nodos button shows a red count badge;
   tooltip names the node, marker, and reason; clicking still opens
   the Nodes modal.
5. After the node recovers (marker cleared, next heartbeat), the badge
   disappears on the next grid refresh.
