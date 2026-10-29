# DEPLOY: numeric-overflow-guard

## What

t148 fixed the tap-coord path, but the SAME `OverflowError` class lurked
in every other numeric conversion of request data. Python's `json.loads`
accepts `Infinity`/`NaN` and arbitrary-precision ints, so a crafted body
killed the handler thread with no response instead of a clean 400:

- Manager `manager/app.py` `_create_instance`: `int(memory_mb)` with
  `Infinity` -> `OverflowError` (except caught only `TypeError`/`ValueError`).
- Manager `_fleet_validate`: `int(count)` / `int(memory_mb)` with
  `Infinity` -> same dropped connection on fleet-plan and fleet-create.
- Manager `validate_resize_params`: `int(memory_mb)` with `Infinity` ->
  same dropped connection on the resize endpoint.
- Wing agent `wing/agent.py` create/resize routes: `int(cpus)` /
  `int(memory_mb)` with `Infinity` -> the agent thread died, seen
  manager-side as a failed `wing_request` instead of a clean 400.
- "Never raises" sanitizers `clean_agent_resource` / `_fleet_number`:
  `float(10**400)` raises `OverflowError`, breaking `/api/instances`
  on a bad agent payload; `_remote_started_at` `int(Infinity)` likewise;
  heartbeat `recoveries` / `port` ints likewise.

Now `OverflowError` joins the except tuples at all 9 manager sites and
both agent routes. Valid inputs are byte-identical (boundaries, string
numerics, truncation); out-of-range values still fail their range
checks with the same messages as before (a huge int passes `int()`
untouched and fails the range check — still a clean error, never a raise).

Files changed: `manager/app.py` (9 except tuples + comments),
`wing/agent.py` (1.0.43 -> 1.0.44, 2 except tuples + comments).
No dashboard change; no wire protocol change. All VERSION-tuple suites
widened, exact pins moved per the bump convention; remote-autologin
snapshots refreshed to HEAD.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/app.py` and `wing/agent.py`; no new stage files. The wing
agent self-updates to 1.0.44 on the next heartbeat after the manager
deploy; the vultr node additionally needs Penta's installer re-run
only if its agent predates self-update (same re-run already needed
for earlier agent versions).

## Standalone (if apply-all is unavailable)

```sh
# from the repo root, on a session with VPS SSH:
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
scp wing/agent.py root@209.222.10.93:/root/jerusalem/wing/agent.py
ssh root@209.222.10.93 'systemctl restart jerusalem && systemctl is-active jerusalem'
```

## Verify

- `python3 pending-deploy/numeric-overflow-guard/test_numeric_overflow_guard.py`
  -> `PASS=43 FAIL=0`.
- After deploy: POST a resize with `{"memory_mb": Infinity}` (raw JSON
  literal, not a string) -> clean 400, connection stays alive; a valid
  resize still applies. Dashboard fleet bar + instance cards unaffected.
- Wing nodes report agent 1.0.44 in the Nodes modal after one heartbeat.
