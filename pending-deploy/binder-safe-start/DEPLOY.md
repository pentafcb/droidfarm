# Deploy binder-safe-start (manager + wing agent 1.0.33)

Rides the apply-all code queue from HEAD — no standalone scp needed and no
new stage files (`manager/app.py` and `wing/agent.py` are already in the
stage list).

## Via the queue (normal path)

From an SSH-capable session, in the repo:

```bash
bash pending-deploy/apply-all/deploy-all.sh
```

This runs the preflight (t103: `test_binder_safe_start.py`, 25/25), stages
`manager/app.py` + `wing/agent.py`, deploys to `/root/jerusalem/` on the
VPS, restarts `jerusalem.service`, and verifies.

## Post-deploy behavior (no action needed)

- Local Start/Restart (single buttons and batch "Start all"/"Restart all")
  restore the binderfs + loop device pools before `docker start`, so a
  start issued after a host reboot no longer wedges the container with
  docker-created stub dirs at the vanished bind sources.
- The wing agent's `/start` endpoint does the same via
  `_sw_prepare_pools_for_start()` before `docker("start", ...)`.
- The manager's wing-agent-update mechanism advertises 1.0.33 in the next
  heartbeat response; each wing agent verifies the sha256, swaps the file,
  and exits so systemd restarts it on the new code.
- Both prepare calls are best-effort and guarded at the call site: a
  failure can never block the start itself.

## Verify

1. `pending-deploy/apply-all/deploy-all.sh --stage-only` green (t103 present).
2. On the VPS: `grep -c _prepare_pools_for_start /root/jerusalem/app.py` > 0
   and `grep -c _sw_prepare_pools_for_start /root/jerusalem/wing/agent.py` > 0.
3. Dashboard Nodes modal: agent version shows 1.0.33 after the next heartbeat.

## Rollback

Standard apply-all rollback (vps-apply.sh restores the pre-deploy backups
and restarts). Behavior change is strictly additive (a pre-start pool
restore); rolling back restores the old raw-`docker start` behavior.
