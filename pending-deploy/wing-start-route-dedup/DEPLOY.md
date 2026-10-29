# Deploy wing-start-route-dedup (wing agent 1.0.34)

Rides the apply-all code queue from HEAD — no standalone scp needed and no
new stage files (`wing/agent.py` is already in the stage list).

## Via the queue (normal path)

From an SSH-capable session, in the repo:

```bash
bash pending-deploy/apply-all/deploy-all.sh
```

This runs the preflight (t106: `test_wing_start_route_dedup.py`, 9/9),
stages `wing/agent.py`, deploys to `/root/jerusalem/` on the VPS, restarts
`jerusalem.service`, and verifies.

## Post-deploy behavior (no action needed)

- The wing agent's dead `(start|stop)` route alternative is gone: do_POST
  now has exactly one `/start` route (with `_sw_prepare_pools_for_start()`,
  unchanged) and one `/stop` route (unconditional `docker("stop", ...)`).
  Live behavior is identical — the removed `start` branch was unreachable
  (shadowed by the dedicated `/start` route).
- The manager's wing-agent-update mechanism advertises 1.0.34 in the next
  heartbeat response; each wing agent verifies the sha256, swaps the file,
  and exits so systemd restarts it on the new code.

## Verify

1. `pending-deploy/apply-all/deploy-all.sh --stage-only` green (t106 present).
2. On the VPS: `grep -c '(start|stop)' /root/jerusalem/wing/agent.py` == 0
   and `grep -m1 '^VERSION' /root/jerusalem/wing/agent.py` shows 1.0.34.
3. Dashboard Nodes modal: agent version shows 1.0.34 after the next heartbeat.
4. `POST /api/agent/instances/<n>/stop` still stops; `/start` still
   pool-prepares then starts (unchanged paths).

## Rollback

Standard apply-all rollback (vps-apply.sh restores the pre-deploy backups
and restarts). Behavior change is nil (dead-code removal only); rolling
back restores the shadowed duplicate route, which was never reachable.
