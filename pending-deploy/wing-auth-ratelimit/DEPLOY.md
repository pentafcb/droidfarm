# Deploy: wing-auth-ratelimit (agent 1.0.19)

Rides `apply-all` from HEAD — `wing/agent.py` is already in the stage list.
No new staged files, no VPS SSH needed for this item itself.

## Via apply-all (normal path)

From an SSH-capable session, in the repo:

```bash
bash pending-deploy/apply-all/deploy-all.sh        # old VPS (default target)
# or
bash pending-deploy/newbox-sync/sync-newbox.sh --yes   # new VPS 64.177.49.68
```

This carries agent 1.0.19 to `/root/jerusalem/wing/agent.py` (the served
installer source). Verify on the VPS:

```bash
grep -n 'VERSION = "1.0.19"' /root/jerusalem/wing/agent.py
grep -c 'too many attempts' /root/jerusalem/wing/agent.py   # >= 3 (GET/POST/DELETE)
```

## Wing node (vultr)

The vultr node gets agent 1.0.19 when Penta re-runs the node installer
(same re-run already pending for 1.0.2-1.0.18). Until then its agent
accepts unlimited key attempts — the manager's `>=1.0.2` gate accepts
1.0.19 and nothing misreports.

## Live behavior check (after the installer re-run)

```bash
# 10 bad keys -> 429 on the 11th (same body as the manager's /api/login 429)
for i in $(seq 1 11); do
  curl -s -o /dev/null -w "%{http_code}\n" -H "X-Agent-Key: wrong" \
    http://127.0.0.1:8090/api/agent/instances
done
# expect: ten 401 lines, then 429
curl -s http://127.0.0.1:8090/api/agent/health   # 200, unauthenticated
```

## Rollback

Re-run apply-all from the previous commit; the agent file is covered by
the orchestrator's automatic backup + rollback.
