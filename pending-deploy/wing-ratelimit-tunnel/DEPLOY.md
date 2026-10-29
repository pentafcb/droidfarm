# Deploy: wing-ratelimit-tunnel (agent 1.0.28)

Rides `apply-all` from HEAD — `wing/agent.py` is already in the stage list.
No new staged files, no VPS SSH needed for this item itself.

## Via apply-all (normal path)

From an SSH-capable session, in the repo:

```bash
bash pending-deploy/apply-all/deploy-all.sh        # old VPS (default target)
# or
bash pending-deploy/newbox-sync/sync-newbox.sh --yes   # new VPS 64.177.49.68
```

This carries agent 1.0.28 to `/root/jerusalem/wing/agent.py` (the served
installer source; the vultr node self-updates from there on the next
heartbeat — no installer re-run needed). Verify on the VPS:

```bash
grep -n 'VERSION = "1.0.28"' /root/jerusalem/wing/agent.py
grep -c '_agent_client_ip' /root/jerusalem/wing/agent.py   # 3 (def + call + docstring)
```

## Live behavior check (after deploy / self-update)

```bash
# A tunneled user (loopback peer + CF-Connecting-IP) gets its own bucket:
for i in $(seq 1 11); do
  curl -s -o /dev/null -w "%{http_code}\n" -H "X-Agent-Key: wrong" \
    -H "CF-Connecting-IP: 203.0.113.71" \
    http://127.0.0.1:8090/api/agent/instances
done
# expect: ten 401 lines, then 429
# A different CF-Connecting-IP is untouched (separate bucket):
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Agent-Key: wrong" \
  -H "CF-Connecting-IP: 203.0.113.72" http://127.0.0.1:8090/api/agent/instances
# expect: 401, not 429
```

## Rollback

Re-run apply-all from the previous commit; the agent file is covered by
the orchestrator's automatic backup + rollback. Vultr nodes keep the old
peer-keyed limiter until their next heartbeat pulls the rolled-back file —
nothing misreports meanwhile (the dashboard's agent-update badge shows
the version).
