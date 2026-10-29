# remote-resize-sink-gate — deploy notes

## What it is

Manager-only hardening, the NEXT CANDIDATE named by the 2026-10-28
~11:49 UTC session and the same audit class as t163 (local resize
sink gate): `resize_remote_instance()` built its wing URL path from
`name` and sent + stored `memory_mb`/`cpus` verbatim, with only a
membership check on the instance record. The resize route validates
today, but the sink itself did not — a future internal caller passing
a hostile name would drive a wing URL before any route gate ever ran,
and uncoerced numerics would be sent to the agent and stored verbatim
in `instances.json`.

Now the sink fails closed at the top, before any provider lookup or
wing call: invalid name -> (404, "instance not found"); uncoercible
or out-of-bounds numerics -> (400, "invalid cpus/memory"); both-None
-> (400, "memory_mb or cpus required"). Bounds match the route
validator exactly (memory 1024-32768 MB, cpus 0.5-32); None means
"keep the existing limit" for that field. Valid names behave
identically — the live caller already coerces. No agent, dashboard,
or wire-protocol change (the agent's own resize route already gates
name + numerics).

## Files changed (in the repo, deployed from HEAD via apply-all)

- `manager/app.py` — the gate + comment only.
- No agent, dashboard, or wire-protocol change.

## Tests

`test_remote_resize_sink_gate.py` — 49/49 checks (hermetic, real
function): regression proof (contract + old URL/payload construction
interpolates hostile values verbatim); hostile-name matrix -> 404
with zero provider lookups, zero wing calls, zero saves; invalid-
numerics matrix (uncoercible, Infinity/NaN, out-of-bounds) -> 400
with zero wing calls/saves; both-None -> 400; valid + boundary +
string-numeric + partial cases -> 200 with exact wing URL, coerced
payload, coerced stored values; node missing/offline, instance
missing, agent failure passthrough (no state write); gate placement
before provider/wing, sibling local sink gate kept, route validator
kept, snapshot parity; negative proof that the pre-fix flow reaches
the wing URL + payload from hostile inputs.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (preflight t168).
No new stage files — `manager/app.py` is already in the apply-all stage
list. Standalone fallback (only if apply-all is unavailable):

```sh
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify after deploy

- `systemctl is-active jerusalem` → `active`.
- Dashboard root returns HTTP 200.
- A corrupted instance name can no longer drive a wing resize URL
  from `resize_remote_instance` (code inspection: the gate is the
  first statement in the function, before `load_providers`).
- Remote resize still works on a real node (needs a live node; the
  valid-name path is unchanged for the route caller, which already
  coerces).
