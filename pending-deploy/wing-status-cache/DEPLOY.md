# wing-status-cache — agent 1.0.20 per-instance status snapshot cache

## What
10s-TTL in-agent cache over `instance_status_linux()`, mirroring the
manager's instance-health-cache (2026-10-10). The manager polls
`/api/agent/instances` every 10s per provider; each poll ran `docker ps -a`
+ `docker inspect -f {{.State.StartedAt}}` once per instance — 2 serialized
subprocess spawns per instance per poll, per connected dashboard client.

## Deploy (rides apply-all from HEAD — no manual steps)
`wing/agent.py` is already in the apply-all stage list. Deploy:

    bash pending-deploy/apply-all/deploy-all.sh          # or sync-newbox.sh --yes

The vultr node gets agent 1.0.20 when Penta re-runs the node installer
(same re-run already pending for 1.0.2–1.0.19); until then srt nodes pay
the old per-poll cost and nothing misreports.

## Verify (after deploy, from the repo machine)
1. `bash pending-deploy/live-verify/verify-live.sh --host=<node> --ssh`
   (or the manager box after sync) — all file probes OK.
2. On the node: `curl -s -H "X-Agent-Key: $KEY" \
   http://127.0.0.1:8090/api/agent/health | grep '"version": "1.0.20"'`
3. Dashboard Nodes modal shows the node healthy; instance cards keep
   refreshing (10s cadence unchanged — the cache TTL matches it).

## Behavior notes
- Mutation routes (create/start/stop/delete) invalidate the entry, so the
  next poll shows fresh state. The selfwatch thread's own restarts ride the
  same 10s window the manager's cache uses — no invalidation there, by
  parity.
- Invalid instance names bypass the cache and call through uncached
  (today's behavior, unchanged).
- All cache helpers are best-effort and never raise; a cache bug degrades
  to the old behavior, never a broken response.
- Zero manager/dashboard changes: response fields are byte-identical.
