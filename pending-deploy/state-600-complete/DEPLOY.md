# state-600-complete — DEPLOY.md

## What
Closes the state-file 600 audit. After providers-600, three saves
were still unhardened:

- `manager/app.py` `save_instances`: NO chmod at all — under umask
  022 `instances.json` sat world-readable, exposing the fleet's
  instance names and internal IPs.
- `manager/app.py` `save_login_results`: chmod AFTER the replace —
  the exact flaw state-tmp-sweep fixed for accounts. A crash
  between replace and chmod left a world-readable tmp behind, and
  the live file was briefly exposed on every save. Outcome-only
  file, but it was the last chmod-after instance in the manager.
- `wing/agent.py` `save_instances`: the same no-chmod save on the
  node side, exposing the node's instance names and internal IPs.

## Fix
All three chmod their tmp to 600 BEFORE the replace (`os.replace`
preserves the tmp's mode, so the live file is 600 from birth),
OSError-guarded so a chmod failure never breaks the save. The next
save after deploy repairs already-deployed files automatically,
since every one of these files is re-saved on normal operation
(instance create/update, login recording, node instance changes).
No agent version bump: no manager gate depends on the agent's
`save_instances` and self-update is sha-driven (same precedent as
remote-b64-sweep), so VERSION stays 1.0.38.

After this change, every `save_*` in `manager/app.py` and the
agent's `save_instances` is chmod-before-replace — the state-file
600 audit is complete. (The agent's `recoveries.json` counter
write is counts only, not state, and is out of scope.)

## Deploy
Rides apply-all from HEAD — `manager/app.py` and `wing/agent.py`
are already in the stage list. No new stage files, no restart
beyond the normal `systemctl restart jerusalem` that apply-all
performs.

Standalone (SSH-capable session, manager host + node hosts pick up
the agent via self-update on the next heartbeat):
```
scp manager/app.py root@<vps>:/root/jerusalem/manager/app.py
ssh root@<vps> 'python3 -m py_compile /root/jerusalem/manager/app.py && systemctl restart jerusalem && systemctl is-active jerusalem'
```

## Verify
- `systemctl is-active jerusalem` -> `active`.
- `stat -c %a /root/jerusalem/instances.json
  /root/jerusalem/roblox_login_results.json` -> `600 600` (pre-fix,
  644; both flip on the next save after deploy).
- `ls /root/jerusalem/instances.json.tmp
  /root/jerusalem/roblox_login_results.json.tmp` -> no match.
- Dashboard instance list still loads; a login attempt still
  records its outcome badge.

## Tests
`python3 pending-deploy/state-600-complete/test_state_600_complete.py`
— hermetic real-app + real-agent import, tmp state dirs, umask 022;
exact-count line `PASS=33 FAIL=0`. Pre-fix regression proof:
`git show HEAD` copies of both files save instances at 644.
