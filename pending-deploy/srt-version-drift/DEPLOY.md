# Deploy: srt-version-drift (wing agent 1.0.5 + manager remote-branch drift)

No standalone scp needed: `wing/agent.py` and `manager/app.py` are already
in the apply-all stage list — this package rides apply-all from repo HEAD.

Deploy (from an SSH-capable session):
```bash
cd ~/workspace/droidfarm
bash pending-deploy/apply-all/deploy-all.sh
```

The deploy scp's `wing/agent.py` -> `/root/jerusalem/wing/agent.py` on the
main VPS and restarts jerusalem. Remote (srt) nodes do NOT get the new agent
automatically: the vultr node still needs Penta to re-run the node installer
so srt's host picks up agent 1.0.5 (same re-run already needed for 1.0.2
auto-login + 1.0.3 self-watchdog + 1.0.4 remote resources). Until then,
remote cards honestly report no drift (False/None) and the manager never
breaks on the missing fields.

Verify:
1. Main VPS: `python3 -c "import sys; sys.path.insert(0,'/root/jerusalem/wing'); import agent; print(agent.VERSION)"` -> 1.0.5
2. Live dashboard: `/api/instances` (auth) — remote entries carry
   `roblox_drift`/`roblox_drift_reason` keys
3. Vultr node (after installer re-run): agent 1.0.5 selfwatch writes
   `<name>.roblox_drift` in the node's selfwatch dir when a client's
   versionName != pinned; the drift badge appears on the srt card
   ("Roblox actualizado — revisa la versión")
4. Force-check without waiting 60s: on the node,
   `docker exec wd-srt1 waydroid shell dumpsys package com.roblox.client |
   grep versionName`

Rollback: re-run apply-all from the previous commit (vps-apply.sh restores
timestamped backups automatically on failure).
