# DEPLOY — create-sink-gate (name + numerics gate on the create sink)

## What it is
`manager/app.py` gains a fail-closed gate at the top of
`create_local_instance`, the shared create core (single-create +
fleet worker) — the follow-up the t161 (login-name-gate) session's
audit class pointed at, on the create path this time.

**The gap**: the HTTP routes all validate create inputs (the
single-create route coerces + bounds cpus/memory_mb, the fleet worker
goes through `_fleet_validate`, the name is stripped or generated)
and `waydroid_exec` / `docker_inspect` gate names at their sinks —
but `create_local_instance` itself did not. Its name, cpus and
memory_mb go straight into the shell `docker run` command, and the
create-ip-gate covers only the fourth interpolation. The
`docker_inspect` gate does NOT stop a hostile name here: it only
makes the name look "missing", and the create then proceeds to
reserve, pass the ip gate, and run docker with the raw name. Blast
radius today was narrow (both live callers validate), but the sink
was wrong regardless, same as the login sinks in t161.

**The fix**: the function fails closed at the top, before any
reservation, docker call, or state write — invalid name ->
`(404, "instance not found")`, uncoercible or out-of-bounds
cpus/memory_mb -> `(400, "invalid cpus/memory")`. Bounds match the
route/fleet validators exactly (cpus 0.5-32, memory 1024-32768 MB);
the coerced values are what the reservation and the docker run see,
which is identical for both live callers (they already coerce). The
agent side needs no change: its `docker()` is list-form (no shell),
and `create_instance_linux` already fullmatch-rejects the name and
the ip. No dashboard change, no wire-protocol change.

## Tests
`test_create_sink_gate.py` — 47/47 pass (real function, stubbed
loads/run/docker helpers): regression proof (contract fact + old
shell construction interpolates hostile name/cpus/memory verbatim),
invalid-name matrix -> 404 with zero docker calls and zero state
writes, invalid-numerics matrix (shell strings, None, NaN/inf,
out-of-bounds, bool) -> 400 with zero docker calls and zero state
writes, valid + boundary + string-numeric cases -> 200 with the
exact docker run fragments and coerced values stored, hostile ip
still 400s after the new gate (ip gate unaffected), source contracts
(gate placement + body indent, ip gate kept, sibling sink gates kept,
route coercion kept, snapshot parity), negative proof (reconstructed
pre-fix flow DOES reach docker with the hostile command).

## Deploy (rides apply-all — no new stage files)
`manager/app.py` is already in the apply-all stage list; the gate
deploys from repo HEAD. Run from an SSH-capable session:

```bash
bash ~/workspace/droidfarm/pending-deploy/apply-all/deploy-all.sh
```

Standalone (if apply-all is unavailable):

```bash
scp ~/workspace/droidfarm/manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem && sleep 2 && systemctl is-active jerusalem'
```
