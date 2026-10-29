# Deploy: resize-cache-invalidate (agent 1.0.36)

Rides `apply-all` from HEAD — `wing/agent.py` is already in the stage list.
No new staged files, no VPS SSH needed for this item itself.

## Via apply-all (normal path)

From an SSH-capable session, in the repo:

```bash
bash pending-deploy/apply-all/deploy-all.sh        # old VPS (default target)
# or
bash pending-deploy/newbox-sync/sync-newbox.sh --yes   # new VPS 64.177.49.68
```

This carries agent 1.0.36 to `/root/jerusalem/wing/agent.py` (the served
installer source; the vultr node self-updates from there on the next
heartbeat — no installer re-run needed). Verify on the VPS:

```bash
grep -n 'VERSION = "1.0.36"' /root/jerusalem/wing/agent.py
# the resize route now invalidates (was missing before 1.0.36):
python3 - <<'EOF'
import re
src = open('/root/jerusalem/wing/agent.py').read()
blk = src[src.index('/api/agent/instances/([^/]+)/resize'):]
blk = blk[:blk.index('def do_DELETE')]
assert 'invalidate_agent_status_cache(name)' in blk, "resize invalidation missing"
print("resize route invalidates: OK")
EOF
```

## Live behavior check (after deploy / self-update)

Hard to observe directly (the 10s snapshot staleness it fixes is
sub-second-visible at most): resize a node instance from the dashboard
(Recursos button), then immediately poll the agent status route twice —
the second poll must not serve a pre-resize cached snapshot:

```bash
# on the wing node (agent key from the manager's providers.json):
curl -s -H "X-Agent-Key: <key>" \
  http://127.0.0.1:8090/api/agent/instances/srt1 | python3 -m json.tool
```

## Rollback

Re-run apply-all from the previous commit; the agent file is covered by
the orchestrator's automatic backup + rollback. Vultr nodes keep agent
1.0.36 until their next heartbeat pulls the rolled-back file — nothing
misreports meanwhile (the dashboard's agent-update badge shows the
version).
