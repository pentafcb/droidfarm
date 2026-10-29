# Deploy wing-boot-recovery (agent 1.0.32)

Rides the apply-all code queue from HEAD — no standalone scp needed and no
new stage files (`wing/agent.py` is already in the stage list).

## Via the queue (normal path)

From an SSH-capable session, in the repo:

```bash
bash pending-deploy/apply-all/deploy-all.sh
```

This runs the preflight (t102: `test_wing_boot_recovery.py`, 85/85), stages
`wing/agent.py`, deploys to `/root/jerusalem/wing/agent.py` on the VPS,
restarts `jerusalem.service`, and verifies.

## Post-deploy behavior (no action needed)

- The manager's wing-agent-update mechanism advertises 1.0.32 in the next
  heartbeat response; each wing agent verifies the sha256, swaps the file,
  and exits so systemd restarts it on the new code.
- On the next host reboot of a wing node, the selfwatch's first pass runs
  section 0: binder pool restore + staggered fleet starts (3/pass).

## Verify

1. `pending-deploy/apply-all/deploy-all.sh --stage-only` green (t102 present).
2. On the VPS: `grep -c sw_boot_recovery /root/jerusalem/wing/agent.py` > 0.
3. Dashboard Nodes modal: agent version shows 1.0.32 after the next heartbeat.
4. The next node reboot: `selfwatch.log` shows the "boot recovery: fresh host
   boot" line and staggered "started ok" lines (3 per pass), then
   "boot recovery complete".

## Rollback

Standard apply-all rollback (vps-apply.sh restores the pre-deploy backup of
`wing/agent.py` and restarts). The agent's own update-rollback also guards a
bad self-update.
