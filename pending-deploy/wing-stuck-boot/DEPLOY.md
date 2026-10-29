# DEPLOY — wing-stuck-boot (wing agent 1.0.14)

## What this deploys
`wing/agent.py` 1.0.14: stuck-boot detection + auto-restart in the agent
selfwatch (see README.md) — parity with the main-node watchdog's section 2.
No manager/dashboard changes: restarts log to `selfwatch.log` -> the Eventos
modal, and circuit-breaker DEGRADED surfaces through the existing
selfwatch_state -> heartbeat -> dashboard Nodes "needs attention" badge.

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
   1.0.14 (same re-run already pending for 1.0.2–1.0.13). Until then the
   manager's `>=1.0.2` gates keep working and srt cards behave as before —
   remote nodes simply lack the stuck-boot guard (nothing misreports).
3. Dashboard check: node heartbeats report `"version": "1.0.14"`; the
   Eventos modal shows `<name> stuck booting (no marker after 15m) ->
   docker restart (N/6 this hour)` lines only if a node actually has a
   stuck boot (none expected on healthy nodes).

## Rollback
Standard apply-all rollback (automatic on verify failure).

## Tests
`python3 pending-deploy/wing-stuck-boot/test_wing_stuck_boot.py` — 26/26
hermetic (fake docker, temp state/vol dirs, injected clock). Preflight t69
(exact-count "26 passed, 0 failed").
