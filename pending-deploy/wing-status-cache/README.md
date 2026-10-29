# wing-status-cache

Agent 1.0.20: per-instance status snapshot cache (10s TTL) in `wing/agent.py`
— the tenth "main had it, wing didn't" parity item (the manager got the
equivalent `instance-health-cache` on 2026-10-10).

## The gap
`/api/agent/instances` feeds the manager's every-10s remote fetch
(dashboard polls). Each poll ran, per instance:
- `docker ps -a` (full scan, via `container_status`)
- `docker inspect -f {{.State.StartedAt}}` (boot time)

2 serialized subprocess spawns per instance per poll, from every connected
dashboard client. A wing node with 5 instances + 2 viewers = 20 docker
spawns per 10s window just for the status grid.

## The fix
- `_agent_status_cache` (name → (epoch, snapshot)), `_AGENT_STATUS_TTL = 10.0`
  (matches the dashboard cadence), stale entries (>60s) pruned on write.
- `cached_instance_status_linux(name)`: hit → fresh `dict()` copy (callers
  can't poison the cache); miss → live sources. Invalid names bypass the
  cache and call through uncached (today's behavior). Never raises.
- `invalidate_agent_status_cache(name)`: wired into the create, start,
  start|stop, and delete routes.
- do_GET `/api/agent/instances` loop and `/api/agent/instances/{name}/status`
  both use the cached wrapper. The four manager call sites of the single
  status route (started_at read, login gating, boot-wait loop, batch remote
  check) all tolerate 10s staleness.

## Tests
`test_wing_status_cache.py` — 28/28 hermetic (fake docker, fake time,
fake marker files): version + gate, constants, key units, miss/hit/TTL/
isolation, snapshot content (ready/booting/stopped/drift), invalid-name
passthrough, invalidation, never-raises (raising docker + garbage), prune,
10-thread smoke, route wiring contracts.
