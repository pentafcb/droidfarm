# DEPLOY — login-name-gate (name gate on the Roblox login path + input sink)

## What it is
`manager/app.py` gains a name gate on the two login-path sinks that
lacked one — the follow-up the t160 (watchdog-name-gate) session's
audit class pointed at, on the manager side this time.

**The gap**: the manager's HTTP routes all gate instance names
(route-name-gate) and `waydroid_exec` / `docker_inspect` gate them at
the sink — but `_roblox_login_inner` and `_send_input_cmds` did not.
`_roblox_login_inner` writes the cleartext password file (`pwd.txt`)
and the login script (`auto_login.sh`) into a volume path built
straight from the name, then taps the login coordinates; it is
reached from the auto-login watcher and the batch-login worker, which
iterate `instances.json` keys — a corrupted key (the same class the
t154/t155/t156 record-shape guards were built for) could drive those
writes and taps. `_send_input_cmds` builds its FIFO path straight
from the name and opens it, with the same internal callers. The
dashboard Login route gates its name, but the internal callers never
did. Blast radius today was narrow (HTTP routes gated; a corrupted
key still needs a valid account record to reach the writes; the
traversal math lands mostly in missing directories, failing clean) —
but the sinks were wrong regardless, same as the watchdog's.

**The fix**: both functions fail closed on an invalid name before
touching anything — `_roblox_login_inner` returns
`(False, "instance not found")` and `_send_input_cmds` returns
`False`. Valid names behave identically; the `roblox_login()` wrapper
still records the outcome (`record_login_result` sanitizes names).
No agent change, no dashboard change, no wire-protocol change.

## Tests
`test_login_name_gate.py` — 34/34 pass (real functions, stubbed
loads/FIFO/run): regression proof (every attack-shaped name fails the
gate contract; the old FIFO-path construction normalizes a traversal
name out of its per-instance directory), invalid-name matrix blocked with zero FIFO opens / zero wing
calls / zero files created even with a valid account record and a
remote-looking provider, valid names still reach the FIFO and
fallback paths, valid-name full login run still completes and leaves
no password file or script behind, wrapper records the gated outcome
for `by="auto"` and `by="batch"`, source contracts (gate placement,
wrapper recording, sibling gates kept, snapshot parity), negative
proof (reconstructed pre-fix FIFO path does touch the filesystem for
a traversal name).

## Deploy (rides apply-all — no new stage files)
`manager/app.py` is already in the apply-all stage list; the gate
deploys from repo HEAD. Run from an SSH-capable session:

```bash
bash ~/workspace/droidfarm/pending-deploy/apply-all/deploy-all.sh
```

Standalone (if apply-all is unavailable):

```bash
scp ~/workspace/droidfarm/manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem && sleep 2 && systemctl is-active jerusalem'
```

## Verify on the VPS
- `python3 -m py_compile /root/jerusalem/app.py` → clean.
- `grep -n "Sink gate" /root/jerusalem/app.py` → gate comments present
  in `_roblox_login_inner` and `_send_input_cmds`.
- Dashboard login on a real instance still works (valid names
  unaffected); a corrupt instance key can no longer drive login
  writes (proved hermetically — see the suite).
