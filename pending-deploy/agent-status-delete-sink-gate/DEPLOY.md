# agent-status-delete-sink-gate — deploy notes

## What it is

Agent-side sink census (the audit the 2026-10-28 ~15:45 UTC session
pointed at): the manager's sink-gate chain is closed end to end
(t161–t169), and the wing agent's sinks were spot-checked as gated in
past sessions but never census-swept the way the manager's were. The
census found two route-gated-only sinks:

- `instance_status_linux()` — the LIVE one. The /status route gates
  names, but the /api/agent/instances loop calls
  `cached_instance_status_linux()` on `load_instances()` keys with NO
  route gate between them, and invalid names bypass that cache and call
  through to this sink. A corrupted node `instances.json` key therefore
  reached it on every 10s manager poll, building a volume path from the
  raw name (a traversal resolves into another instance's volume
  markers) and running a docker inspect on wd-<name> before the
  (gated) drift/degraded readers ever ran.
- `delete_instance_linux()` — the sibling. Its DELETE route gates
  names, but the sink itself drove docker rm / volume rm / state pop
  from the raw name, so a future internal caller passing a corrupted
  key would delete before any route gate ever ran.

Both now fail closed at the top, before any fs/docker/state touch:
status returns the stopped-shaped dict (identical to its not-running
branch, no started_at), delete returns False (old code always returned
True; the DELETE route already 404s invalid names, so live behavior is
unchanged). Valid names behave identically.

Census result for the record (all other name sinks already gated at
the sink): create, resize, screenshot, input, launch, focus, b64
staging, stream_wanted, drift check/read, degraded read, and every
selfwatch helper (booted_once, session, stuck_boot, keepalive,
install_failed, check_instance, drop_selfwatch_state); the status
cache key gate already rejected invalid names from the cache itself.

## Files changed (in the repo, deployed from HEAD via apply-all)

- `wing/agent.py` — the two gates + a docstring correction on the
  cached wrapper only. No manager, dashboard, or wire-protocol change.
- Deliberately NO agent VERSION bump: the manager's agent gate is
  >= 1.0.2, and bumping would churn 53 test files' VERSION assertions
  for zero behavioral gain. The apply-all MANIFEST hash distinguishes
  the deployed agent.py; VERSION stays 1.0.45.

## Tests

`test_agent_status_delete_sink_gate.py` — 121/121 checks (hermetic,
real functions, stubbed docker): regression proof (a traversal name
resolves into a different instance's volume), status hostile-name
matrix -> stopped dict with zero docker calls, valid stopped/running
paths unchanged (exact dict shape, exact docker calls, mem/cpu from
stats, started_at parsed), cached wrapper (hostile -> stopped dict +
no cache entry; valid -> cached, second call makes no new docker
calls), delete hostile matrix -> False with zero docker/state calls,
delete valid -> True with exact rm + volume rm calls, state pop +
save, selfwatch markers dropped, gate placement + sibling sink gates
kept + route gates kept + deploy wiring, negative proof.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (preflight
t170). No new stage files — `wing/agent.py` is already in the
apply-all stage list. The vultr node additionally needs Penta to
re-run the node installer so srt1's host gets the new agent.py (same
re-run already needed for agent 1.0.2/1.0.3). Standalone fallback
(only if apply-all is unavailable):

```sh
scp wing/agent.py root@209.222.10.93:/root/jerusalem/wing/agent.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify after deploy

- `systemctl is-active jerusalem` → `active`.
- Dashboard root returns HTTP 200.
- Remote node instances still report status (valid names unchanged);
  a corrupted instance key on a node can no longer drive a volume
  path or docker inspect from the status sink, or a docker rm from
  the delete sink (hermetic suite proves the fix).
