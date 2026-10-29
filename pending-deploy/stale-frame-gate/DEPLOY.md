# DEPLOY.md — stale-frame-gate

## Rides apply-all (preferred)

No new stage files: `manager/app.py` + `wing/agent.py` are already in
the apply-all stage list. Deploy = run
`pending-deploy/apply-all/deploy-all.sh` from an SSH-capable session
(preflight t142 runs this package's suite).

Wing nodes pick up agent 1.0.40 (VERSION bumped in `wing/agent.py`
with this change, so the heartbeat self-update directive fires) after
the manager deploy; until then, remote instances keep the old
behaviour (frozen frame served) while local instances are already
gated.

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
3. A live viewer still streams normally (fresh frames are <10s old;
   the gate never fires on a healthy loop).
4. Regression proof (needs a stopped screencap loop, SSH session):
   stop one instance's screencap loop, wait ~15s, open its viewer —
   the stall badge ("Imagen congelada") must appear and the stream
   must fail over / reconnect instead of sitting on a frozen frame
   labelled live. The watchdog's 30s stale-stream restart then
   repairs the loop, as before.

## Rollback

Re-deploy the previous `app.py` + `wing/agent.py` from the apply-all
backups (`/root/jerusalem-deploy/` timestamps) and restart jerusalem.
No state, data, or credentials are touched by this change.
