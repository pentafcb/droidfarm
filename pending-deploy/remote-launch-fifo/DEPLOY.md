# remote-launch-fifo — DEPLOY.md

## What
Remote "Launch Roblox" never worked: the wing agent wrote the launch
sequence as five separate FIFO writes, but the input daemon reads one
line per FIFO open, so only HOME ever executed (and the writer could
EPIPE mid-loop). The local path was fixed to a compound write in
October; this is the remote-path parity fix. After deploy, tapping
"Launch Roblox" on a wing-node instance (or fleet "Roblox all") opens
the drawer and taps the Roblox icon, like local instances already do.

## Fix
- `wing/agent.py` `launch_roblox_linux()`: one compound
  `"; ".join(cmds)` write per FIFO open; sequence aligned to the
  manager's verified local sequence (HOME, sleep 1, drawer swipe,
  sleep 2, tap 129,424 on 1024x608).
- NO agent version bump (no manager gate on launch; self-update is
  sha-driven — see README.md).

## Deploy
Rides apply-all from HEAD — `wing/agent.py` is already in the stage
list. No new stage files, no restart beyond the normal
`systemctl restart jerusalem` that apply-all performs. After the
manager deploy, wing nodes self-update to the fixed agent on their
next heartbeat (no installer re-run needed).

Standalone (SSH-capable session, manager host):
```
scp wing/agent.py root@<vps>:/root/jerusalem/wing/agent.py
ssh root@<vps> 'python3 -m py_compile /root/jerusalem/wing/agent.py && systemctl restart jerusalem && systemctl is-active jerusalem'
```

## Verify
- From the dashboard, open a wing-node instance viewer and tap
  "Launch Roblox": Roblox opens (previously only the home screen
  showed). Fleet "Roblox all" likewise launches on remote instances.
- The agent self-update lands on the node after the next heartbeat:
  the node's reported agent version stays 1.0.38 (no bump) but the
  file sha matches the deployed `wing/agent.py`.

## Tests
`python3 pending-deploy/remote-launch-fifo/test_remote_launch_fifo.py`
— hermetic real-agent import + real FIFO with a daemon-faithful
reader; exact-count line `PASS=19 FAIL=0`.
