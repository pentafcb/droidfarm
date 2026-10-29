# DEPLOY: remote-fetch-exception-cache

## What

One-line-class fix in `manager/app.py`: `fetch_remote_statuses()` now
serves the 30s last-known-good cache when `wing_request` raises
(timeout, connection error), exactly like it already did for a non-200
response (see README.md). Without it, a single transient timeout flipped
every card on a wing node to "stopped" at once, while the
node-fetch-badge needs 6 consecutive failures before it says anything.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/app.py`; no new stage files.

## Standalone (if apply-all is unavailable)

```sh
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify

1. `systemctl is-active jerusalem` -> active.
2. Dashboard root returns HTTP 200.
3. Sign in, open the dashboard: with all wing nodes heartbeating and
   answering, remote cards show live state and no new badge appears
   (no dashboard change in this package).
4. Break a wing node's status endpoint so fetches raise (e.g. stop the
   agent's HTTP handler while heartbeats keep flowing, or point the
   manager at a stub that hangs past the 5s timeout): its cards keep
   showing the last-known state for 30s instead of flipping to
   "stopped" on the first failed poll; after ~60s (6 failures) the red
   "Nodo sin respuesta" badge appears as before.
5. When the endpoint answers again, the badge disappears on the next
   grid refresh (10s poll) and the cards show live state again.
