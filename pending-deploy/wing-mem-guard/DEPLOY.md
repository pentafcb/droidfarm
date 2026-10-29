# DEPLOY — wing-mem-guard (wing agent 1.0.13)

## What this deploys
`wing/agent.py` 1.0.13: host-level memory-pressure guard + OOM surfacing in
the agent selfwatch (see README.md) — parity with the main-node watchdog's
v7 section 9. No manager/dashboard changes: `mem.degraded` surfaces through
the existing selfwatch_state -> heartbeat -> dashboard Nodes "needs
attention" badge, and all alerts land in selfwatch.log -> the Eventos modal.

## Deploy path
Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD — `wing/agent.py`
is already in the stage list. No standalone steps needed:

```bash
bash pending-deploy/apply-all/deploy-all.sh          # from an SSH-capable session
```

Then on the VPS: `bash /root/jerusalem/finish-deploy.sh` (unchanged).

## Post-deploy (next SSH-capable session)
1. `verify-live.sh --ssh` — checks the staged agent.py SHA.
2. Wing nodes (vultr): Penta re-runs the node installer to pick up agent
   1.0.13 (same re-run already pending for 1.0.2–1.0.12). Until then the
   manager's `>=1.0.2` gates keep working and srt cards behave as before —
   remote nodes simply lack the memory guard (nothing misreports).
3. Dashboard check: node heartbeats report `"version": "1.0.13"`; the
   Eventos modal shows `WARN memory pressure` lines only if a node is
   actually under pressure (none expected on healthy nodes).

## Rollback
Standard apply-all rollback (automatic on verify failure).
