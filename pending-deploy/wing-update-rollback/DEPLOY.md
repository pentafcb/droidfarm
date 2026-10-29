# Deploy: wing-update-rollback (agent 1.0.30)

Rides `apply-all` from HEAD — `wing/agent.py` is already in the stage list.
No new staged files, no VPS SSH needed for this item itself.

## Via apply-all (normal path)

From an SSH-capable session, in the repo:

```bash
bash pending-deploy/apply-all/deploy-all.sh        # old VPS (default target)
# or
bash pending-deploy/newbox-sync/sync-newbox.sh --yes   # new VPS 64.177.49.68
```

This carries agent 1.0.30 to `/root/jerusalem/wing/agent.py` (the served
installer source; the vultr node self-updates from there on the next
heartbeat — no installer re-run needed). Verify on the VPS:

```bash
grep -n 'VERSION = "1.0.30"' /root/jerusalem/wing/agent.py
grep -c 'maybe_rollback_agent_update' /root/jerusalem/wing/agent.py   # 3 (def + 2 calls)
```

## Live behavior check (after deploy / self-update)

On the node, in the selfwatch state dir (`<DATA_DIR>/selfwatch/`):

```bash
# After any self-update, expect the dated marker:
cat <DATA_DIR>/selfwatch/agent.update.marker   # {"version": "1.0.30", "from": "1.0.29", "ts": ...}
# Boot log grows one line per agent startup:
wc -l <DATA_DIR>/selfwatch/agent.boot.log
# Simulate a crash loop: stop the agent 4x within 5 min (systemctl stop/start
# x4, or kill the process). Expect ONE ALERT "agent rolled back ..." in
# selfwatch.log, agent.py restored from agent.py.bak, and the marker gone:
grep -c "rolled back" <DATA_DIR>/selfwatch/selfwatch.log   # 1
ls <DATA_DIR>/selfwatch/agent.update.marker                # No such file
```

## Rollback

Re-run apply-all from the previous commit; the agent file is covered by
the orchestrator's automatic backup + rollback. Nodes that already
self-updated to 1.0.30 keep it until their next heartbeat pulls the
rolled-back file — nothing misreports meanwhile (the dashboard's
agent-update badge shows the version). A node that rolled itself back to
1.0.29 will re-update to 1.0.30 on a later heartbeat only if the manager
still serves 1.0.30 — after an apply-all rollback it serves 1.0.29 again,
so no update ping-pong.
