# Deploy: wing-missing-script-guard (agent 1.0.29)

Rides `apply-all` from HEAD — `wing/agent.py` is already in the stage list.
No new staged files, no VPS SSH needed for this item itself.

## Via apply-all (normal path)

From an SSH-capable session, in the repo:

```bash
bash pending-deploy/apply-all/deploy-all.sh        # old VPS (default target)
# or
bash pending-deploy/newbox-sync/sync-newbox.sh --yes   # new VPS 64.177.49.68
```

This carries agent 1.0.29 to `/root/jerusalem/wing/agent.py` (the served
installer source; the vultr node self-updates from there on the next
heartbeat — no installer re-run needed). Verify on the VPS:

```bash
grep -n 'VERSION = "1.0.29"' /root/jerusalem/wing/agent.py
grep -c 'sw_warn_helper_missing' /root/jerusalem/wing/agent.py   # 3 (def + 2 calls)
```

## Live behavior check (after deploy / self-update)

On the node, in the selfwatch state dir (`<DATA_DIR>/selfwatch/`):

```bash
# Simulate: stop a helper and remove its script from the data volume, then
# watch one selfwatch cycle (60s). Expect ONE WARN line naming the script
# and "boot setup never installed it", and NO "dead -> restart" line:
tail -f <DATA_DIR>/selfwatch/selfwatch.log
# Expect NO srt1.helper-<name>.ev budget files to appear:
ls <DATA_DIR>/selfwatch/ | grep -c 'helper-.*\.ev$'   # 0
```

## Rollback

Re-run apply-all from the previous commit; the agent file is covered by
the orchestrator's automatic backup + rollback. Vultr nodes keep agent
1.0.28 until their next heartbeat pulls the rolled-back file — nothing
misreports meanwhile (the dashboard's agent-update badge shows the
version).
