# remote-b64-sweep — DEPLOY.md

## What
Remote b64 text staging leaked on agent restart: the wing agent stages
a decoded `/input` b64 payload — in practice a Roblox login password —
in the instance volume as `input-b64.txt` (600, atomic tmp+rename),
deleted ~60s later by a daemon thread. The thread dies with the agent
process, so an agent restart inside the 60s window (crash, self-update,
systemd restart, host reboot) left the staged secret in the volume
forever, readable from inside Android and from the host volume path.
A thread-start failure left it behind too, and a crash between the tmp
write and `os.replace` left `input-b64.txt.tmp` behind. Same leak class
the manager's login-pwd-cleanup closed locally; the remote path had no
sweep at all (no startup sweep, no selfwatch coverage).

## Fix
- `wing/agent.py` `sweep_stale_b64_input_text()`: removes
  `input-b64.txt` + `input-b64.txt.tmp` per instance (name-gated
  paths, never raises, returns the count).
- `main()` sweeps unconditionally at startup, before any thread
  starts (no live stage can exist yet — the server is not bound).
- `selfwatch_check_instance()` sweeps files older than 2x the TTL
  (120s) each pass, alert-only (never affects `healthy`, logged to
  the selfwatch log so the dashboard Eventos modal surfaces it).
- NO agent version bump (no manager gate depends on the sweep;
  self-update is sha-driven — same precedent as remote-launch-fifo).

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
- `systemctl is-active jerusalem` -> `active`.
- On a wing node, stage a b64 input, then restart the agent within
  60s: after the restart, `input-b64.txt` is gone from the instance
  volume (before this fix it persisted indefinitely).
- A stale file older than 120s disappears on the next selfwatch pass
  (60s) with a "stale b64 input staging removed" line in the node's
  selfwatch log; a fresh stage is untouched by that pass.
- The node's reported agent version stays 1.0.38 (no bump) but the
  file sha matches the deployed `wing/agent.py`.

## Tests
`python3 pending-deploy/remote-b64-sweep/test_remote_b64_sweep.py`
— hermetic real-agent import + fake docker selfwatch passes;
exact-count line `PASS=42 FAIL=0`.
