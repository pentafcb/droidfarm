# create-ip-gate

## Problem
`POST /api/instances` accepted an arbitrary `ip` string and
`create_local_instance()` interpolated it into a `shell=True`
`docker run` command line with zero validation — authenticated remote
code execution as root on the manager host
(`{"ip": "10.20.0.5;touch${IFS}/tmp/x"}`).

## Fix (manager/app.py only)
- `valid_ipv4()`: strict dotted-quad check via `ipaddress.IPv4Address`,
  never raises.
- `create_local_instance()`: invalid ip → 400 + reservation rollback,
  before the docker-run string is built. Shared core covers both the
  single-create endpoint and the fleet-create worker.

## Why not at the route
Sink-gate pattern (same as `docker_inspect` / `waydroid_exec`): the
shared core owns the invariant, so both callers are covered and the
gate can't be bypassed by a future caller.

## Tests
`test_create_ip_gate.py` — 98 checks: valid_ipv4 units (valid quads,
malformed, hostile shell payloads, non-string/never-raises), create
wiring (hostile ip → 400 + rollback + docker never invoked; valid ip →
200 with `--ip` in the command; None/"" → generated path), exploit
regression proof (payload bytes never appear in any shell string),
source contracts (gate placement reservation → gate → docker run,
rollback, 400 wording, deadlock-regression indent check, route still
passes ip through).

## Wiring
Preflight t115, exact-count `PASS=52 FAIL=0`. No new stage files —
`manager/app.py` rides apply-all from HEAD.
