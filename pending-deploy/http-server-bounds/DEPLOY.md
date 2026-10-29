# DEPLOY: http-server-bounds (BoundedHTTPServer)

## What
`manager/app.py` and `wing/agent.py` now serve through `BoundedHTTPServer`
instead of stock `ThreadingHTTPServer`:
- max 64 concurrent handler threads (excess connections fail fast with
  HTTP 503 instead of piling up threads until the process wedges);
- every accepted socket gets a 60s inactivity timeout, so half-open or
  dribble-feed clients are dropped instead of pinning a thread forever;
- listen backlog raised to 128.

Root cause it addresses: the 2026-10-11 wedge signature (fast 200 TTFB,
mid-body connection drops) matches a thread-starved server. Both the
dashboard port (:8080) and wing agent ports (8090-8099) are internet-facing.

Agent VERSION bumped 1.0.7 -> 1.0.8 (no behavioral API change; the manager's
>=1.0.2 gates are unaffected).

## Deploy
Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no new
stage files (`manager/app.py` + `wing/agent.py` already in the stage list).

Standalone (SSH-capable session, VPS root):
```
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem'
```
Wing nodes pick up agent 1.0.8 when Penta re-runs the node installer
(same re-run already pending for 1.0.2-1.0.7).

## Verify
- `systemctl is-active jerusalem` -> active
- Dashboard loads (HTTP 200); streams still work (60s timeout only bites
  idle connections — viewers send frames constantly).
- `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/instances` -> 401
- `verify-live.sh --ssh` covers the file SHAs.
