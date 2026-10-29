# DEPLOY.md — launch-focus-gate

## Rides apply-all (preferred)

No new stage files: `manager/app.py` + `wing/agent.py` are already in
the apply-all stage list. Deploy = run
`pending-deploy/apply-all/deploy-all.sh` from an SSH-capable session
(preflight t143 runs this package's suite).

Wing nodes pick up agent 1.0.41 (VERSION bumped in `wing/agent.py`
with this change, so the heartbeat self-update directive fires) after
the manager deploy; until then, remote instances keep the old
behaviour (blind taps) while local instances are already gated.

## Standalone (if apply-all is unavailable)

From the repo root, as root on the VPS (main node):

```bash
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
scp wing/agent.py root@209.222.10.93:/root/jerusalem/wing/agent.py
ssh root@209.222.10.93 'systemctl restart jerusalem && sleep 2 && systemctl is-active jerusalem'
```

No restart is strictly needed for the agent file alone (wing nodes
pull it via self-update); the restart is for `app.py`.

## Verify (after deploy)

1. `systemctl is-active jerusalem` → `active`.
2. Dashboard root returns HTTP 200.
3. Open a viewer on an instance with Roblox already running: the
   viewer must show the game WITHOUT yanking it to the home screen
   (no HOME/drawer sequence), and the viewer status must read
   "Roblox abierto".
4. Open a viewer on an instance sitting at the home screen: Roblox
   must still launch as before (the gate only skips taps when Roblox
   is already focused).
5. Fleet "Roblox all" on a fleet with games already running must not
   restart any of them.

## Rollback

Re-deploy the previous `app.py` + `wing/agent.py` from the apply-all
backups (`/root/jerusalem-deploy/` timestamps) and restart jerusalem.
No state, data, or credentials are touched by this change.
