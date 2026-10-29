# providers-600 — DEPLOY.md

## What
`save_providers` was the one credential-bearing manager save never
chmodded. `providers.json` holds every node's `agent_key` — the
credential the manager presents to wing agents (`X-Agent-Key`) and the
key agents use to pull self-updates — yet under umask 022 the live file
(and any crashed `providers.json.tmp` beside it) sat world-readable.
The `/api/providers` response already strips `agent_key`, so the
on-disk file was the only leak path; the state-tmp-sweep startup sweep
removed stale tmps but never fixed the live file's mode.

## Fix
- `manager/app.py` `save_providers`: chmods its tmp to 600 BEFORE the
  replace (`os.replace` preserves the tmp's mode, so the live file is
  600 from birth), with an OSError guard so a chmod failure never
  breaks the save. No agent change, no version cascade.
- The next heartbeat save after deploy fixes already-deployed files,
  since every heartbeat re-saves providers.

## Deploy
Rides apply-all from HEAD — `manager/app.py` is already in the stage
list. No new stage files, no restart beyond the normal
`systemctl restart jerusalem` that apply-all performs.

Standalone (SSH-capable session, manager host):
```
scp manager/app.py root@<vps>:/root/jerusalem/manager/app.py
ssh root@<vps> 'python3 -m py_compile /root/jerusalem/manager/app.py && systemctl restart jerusalem && systemctl is-active jerusalem'
```

## Verify
- `systemctl is-active jerusalem` -> `active`.
- `stat -c %a /root/jerusalem/providers.json` -> `600` (pre-fix, 644
  permanently; after deploy it flips on the next node heartbeat).
- `ls /root/jerusalem/providers.json.tmp` -> no match.
- Dashboard Nodes modal still lists nodes, and a node heartbeat still
  succeeds (agent_key still saves/loads round-trip).

## Tests
`python3 pending-deploy/providers-600/test_providers_600.py`
— hermetic real-app import, tmp state dir, umask 022;
exact-count line `PASS=18 FAIL=0`.
