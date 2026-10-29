# DEPLOY — agent-thread-supervisor (wing agent 1.0.10)

## What this deploys
`wing/agent.py` 1.0.10: in-agent supervisor for the heartbeat + selfwatch
background threads (see README.md). No manager/dashboard changes.

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
   1.0.10 (same re-run already pending for 1.0.2–1.0.9). Until then the
   manager's `>=1.0.2` gates keep working and srt cards behave as before.
3. Dashboard check: node heartbeats report `"version": "1.0.10"`;
   Eventos modal will show supervisor WARN/ALERT lines if a node thread
   ever dies (none expected on healthy nodes).

## Rollback
Standard apply-all rollback (automatic on verify failure).
