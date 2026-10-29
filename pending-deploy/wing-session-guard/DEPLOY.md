# DEPLOY — wing-session-guard (wing agent 1.0.11)

## What this deploys
`wing/agent.py` 1.0.11: Waydroid session-death detection + auto-restart in
the agent selfwatch (see README.md) — parity with the main-node watchdog's
v9 check. No manager/dashboard changes: DEGRADED trips surface through the
existing selfwatch_state -> heartbeat -> dashboard Nodes "needs attention"
badge, and recoveries surface in the Eventos modal via selfwatch.log.

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
   1.0.11 (same re-run already pending for 1.0.2–1.0.10). Until then the
   manager's `>=1.0.2` gates keep working and srt cards behave as before —
   remote nodes simply lack the session guard (nothing misreports).
3. Dashboard check: node heartbeats report `"version": "1.0.11"`; the
   Eventos modal will show `waydroid session dead -> session start` lines
   if a node session ever dies (none expected on healthy nodes).

## Rollback
Standard apply-all rollback (automatic on verify failure).
