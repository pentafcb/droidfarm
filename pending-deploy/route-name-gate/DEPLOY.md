# DEPLOY — route-name-gate (shell-injection guard)

## What
Fixes authenticated root RCE via shell metacharacters in instance-name URL
segments. Six manager routes passed the raw `([^/]+)` capture into
`shell=True` docker commands; the gate (`valid_instance_name`,
`^[A-Za-z0-9_-]{1,32}$` — the exact create-time contract) now 404s anything
else, and `docker_inspect`/`waydroid_exec` gate as defense in depth.

## Deploy
Rides apply-all from HEAD — `manager/app.py` is already in the stage list.
No new stage files.

Standalone (SSH-capable session only):
```
scp manager/app.py root@<host>:/root/jerusalem/app.py
ssh root@<host> 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem'
```

## Verify
1. `systemctl is-active jerusalem` -> active
2. Dashboard root -> HTTP 200, login works
3. Authenticated: `GET /api/instances/rbx1` (or any name) still 200 with the
   fleet; `GET /api/instances/rbx1;id/roblox` -> 404 `{"error":"instance not found"}`
   (no shell spawned — check no stray files/processes)
4. `DELETE /api/instances/nonexistent-ok-name` -> 404 (not 500)
5. No dashboard change needed (no new UI); JS gate unaffected.

## Rollback
Restore the previous `app.py` from the apply-all backup dir and
`systemctl restart jerusalem`.
