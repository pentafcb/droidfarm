# Deploy: wing-install-guard (agent 1.0.35)

Rides `apply-all` from HEAD — `wing/agent.py` is already in the stage list.
No new staged files, no VPS SSH needed for this item itself.

## Via apply-all (normal path)

From an SSH-capable session, in the repo:

```bash
bash pending-deploy/apply-all/deploy-all.sh        # old VPS (default target)
# or
bash pending-deploy/newbox-sync/sync-newbox.sh --yes   # new VPS 64.177.49.68
```

This carries agent 1.0.35 to `/root/jerusalem/wing/agent.py` (the served
installer source; the vultr node self-updates from there on the next
heartbeat — no installer re-run needed). Verify on the VPS:

```bash
grep -n 'VERSION = "1.0.35"' /root/jerusalem/wing/agent.py
grep -c 'sw_check_install_failed' /root/jerusalem/wing/agent.py   # 3 (defs + call)
```

## Live behavior check (after deploy / self-update)

On a wing node, simulate a failed install in the selfwatch state dir
(`<DATA_DIR>/selfwatch/`) — write `roblox_install_failed` with a reason
into the instance's data volume (the same
`.../data/media/0/jerusalem/` dir the keepalive mirror reads):

```bash
echo "pm install failed: Failure [INSTALL_FAILED_INVALID_APK]" \
  > /var/lib/docker/volumes/wd-srt1-data/_data/data/media/0/jerusalem/roblox_install_failed
# watch one selfwatch cycle (60s):
tail -f <DATA_DIR>/selfwatch/selfwatch.log
# Expect ONE line: "srt1 DEGRADED: Roblox install failed (...) - needs human"
# Expect <DATA_DIR>/selfwatch/srt1.degraded with
#   "Roblox install failed: pm install failed: ..." on the first line
# The dashboard Nodes modal shows the "needs attention" badge on that node.
```

Clear the volume marker and watch the next pass: the mirror auto-clears
and the log shows "recovered from DEGRADED". (Pre-v15 images never write
the volume marker, so the check is a silent no-op there.)

## Rollback

Re-run apply-all from the previous commit; the agent file is covered by
the orchestrator's automatic backup + rollback. Vultr nodes keep agent
1.0.35 until their next heartbeat pulls the rolled-back file — nothing
misreports meanwhile (the dashboard's agent-update badge shows the
version).
