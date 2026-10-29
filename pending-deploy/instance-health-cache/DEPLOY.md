# Deploy: per-instance docker snapshot cache (10s TTL)

Every dashboard poll (`GET /api/instances`, one per 10s per client) ran one
`docker inspect` per local instance — 20 serialized subprocess spawns on a
full fleet, adding seconds of latency to every grid refresh. The manager now
keeps one cached snapshot per instance (docker status + IP + booted/
roblox_done markers + watchdog health) for 10s, matching the dashboard's
own refresh cadence. Start/stop/restart/delete invalidate the entry, so the
next poll shows fresh state.

Response shape is unchanged: same fields, same values, just fresher reads
behind them. Mutation endpoints (single-instance and fleet batch, which
shares `instance_single_action`) invalidate; the fleet "Create fleet" and
login paths are unaffected (login results are loaded live per poll).

## What rides apply-all

No new stage files: `manager/app.py` is already in the apply-all stage list,
and the snapshot cache rides it from repo HEAD. Suite `t28` in preflight:
`pending-deploy/instance-health-cache/test_instance_health_cache.py` (37/37).

## Standalone deploy (one box, no queue)

```bash
# from a machine with VPS SSH:
scp ~/workspace/droidfarm/manager/app.py \
    root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 \
  'python3 -m py_compile /root/jerusalem/app.py && \
   systemctl restart jerusalem'
```

## Verify

- `systemctl is-active jerusalem` -> `active`
- `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/` -> `200`
- Authenticated `GET /api/instances` returns the same fields as before
  (spot-check one card: status/setup/ip/degraded/login badges unchanged).
- Freshness: stop one instance via the dashboard, refresh within 10s — the
  card shows "stopped" (invalidation works); start it again, next refresh
  shows "running" (or the correct setup state).
- Load: with N clients polling, `docker inspect` churn on the host drops
  roughly N-fold (compare `journalctl` / process counts during two parallel
  dashboard sessions).

## Notes

- TTL is `_SNAPSHOT_TTL = 10` in `manager/app.py`; the dashboard polls at
  10s, so staleness never exceeds the natural refresh cadence.
- Remote (wing) instances are untouched: the remote branch never used
  docker inspect and still reports live from the agent.
- All helpers are best-effort and never raise — a cache bug can never break
  the response (worst case: a fresh snapshot every poll, i.e. old behavior).
