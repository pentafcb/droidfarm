# DEPLOY — resize-sink-gate (numerics gate on the resize sink)

## What it is
`manager/app.py` gains a fail-closed gate at the top of
`resize_local_instance`, the shared local-resize core — the follow-up
the t162 (create-sink-gate) session's audit class pointed at, on the
resize path this time.

**The gap**: the resize route validates inputs
(`validate_resize_params` coerces + bounds memory_mb/cpus) and
`resize_local_instance` already gated the NAME at the sink — but it
did NOT gate the numerics. memory_mb and cpus go straight into the
shell `docker update` command, and are written verbatim into
`instances.json` on success. A future internal caller passing a
hostile string (or an uncoerced value, e.g. "2048" stored as a
string) would reach the shell verbatim. Blast radius today was narrow
(the live caller validates), but the sink was wrong regardless, same
as the create sink in t162.

**The fix**: the function coerces + bounds at the top, before any
docker call or state write — uncoercible or out-of-bounds ->
`(400, "invalid cpus/memory")`, both-None ->
`(400, "memory_mb or cpus required")`. Bounds match the route
validator exactly (memory 1024-32768 MB, cpus 0.5-32); None keeps the
existing limit for that field. The coerced values are what the docker
update, the shrink check, and the state write see, which is identical
for the live caller (it already coerces). The agent side needs no
change: its resize route already coerces + bounds and its `docker()`
is list-form (no shell). No dashboard change, no wire-protocol change.

## Tests
`test_resize_sink_gate.py` — 45/45 pass (real function, stubbed
loads/run/docker helpers): regression proof (contract fact + old
shell construction interpolates hostile memory/cpus verbatim),
invalid-numerics matrix (shell strings, NaN/inf, out-of-bounds, bool
memory, list/dict) -> 400 with zero docker calls and zero state
writes, both-None -> 400, valid + boundary + string-numeric cases ->
200 with the exact docker fragments and coerced values stored,
partial resizes (memory-only / cpus-only) carry only that flag and
preserve the other field, shrink-below-usage still refused, name gate
kept, source contracts (gate placement, RESIZE_* constants, sibling
sink gates kept, route validator kept, snapshot parity), negative
proof (reconstructed pre-fix flow DOES reach docker with the hostile
command).

## Deploy (rides apply-all — no new stage files)
`manager/app.py` is already in the apply-all stage list; the gate
deploys from repo HEAD. Run from an SSH-capable session:

```bash
bash ~/workspace/droidfarm/pending-deploy/apply-all/deploy-all.sh
```

Standalone (if apply-all is unavailable): scp `manager/app.py` to
`/root/jerusalem/app.py` on the VPS, then `python3 -m py_compile`
it and `systemctl restart jerusalem`, and verify the service is
active. A resize with a hostile memory/cpus value must then return
400 without touching docker; a valid resize still applies live via
`docker update` with no restart.
