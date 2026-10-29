# fleet-cpu-headroom — CPU overcommit warning in the Create-fleet plan

## Problem
The fleet-create plan preview (`fleet_headroom()`) warned on memory
overcommit, binder/loop shortfalls, and a missing Waydroid image — but
never on CPU. The plan takes a per-instance `cpus` limit and never summed
it against host CPUs, so on the 8-vCPU box, creating 5 more 2-CPU
instances silently pushed CPU limits past 2x the core count with zero
indication. Docker `--cpus` is a quota (shares), not a reservation, so
this never hard-fails — it just makes every instance slower under load.

## Change (manager/app.py only)
- `fleet_headroom(instances, provider_id, count, memory_mb, cpus=None)`:
  new fields `host_cpus` / `current_cpus` / `new_cpus` / `cpu_overcommit`.
  - Local branch: `host_cpus` from `os.cpu_count()`, current CPU limits
    summed from instances.json (same filter as the memory sum).
  - Remote branch: `host_cpus` from the node's heartbeat stats
    (`cpu_count`, carried by the agent's `get_host_stats()` since long
    before this package — no agent change needed).
  - Warning (never a refusal): "CPU limits would exceed host CPUs by N
    (X CPU limits on Y CPU host). CPU limits are shares, not
    reservations — expect slower instances under load, not hard
    failures."
  - `cpus=None` (legacy 4-arg callers): CPU section inert — no warning,
    so the existing fleet-create/remote-headroom suites keep passing.
  - Fail-open: `host_cpus == 0` (unknown) → no warning; garbage cpus →
    the outer best-effort `except` keeps the old fail-open contract.
- `fleet_plan()` passes the validated per-instance `cpus` through.
- Dashboard: no change — the plan modal already renders
  `headroom.warning`, and the CPU warning rides that string.

## Deploy
Rides apply-all from HEAD (no new stage files — `manager/app.py` already
in the stage list).

Standalone (SSH-capable session):
```
scp manager/app.py root@<host>:/root/jerusalem/app.py
ssh root@<host> 'systemctl restart jerusalem && sleep 2 && systemctl is-active jerusalem'
```
Verify: dashboard → Crear flota → plan a count that pushes CPU limits
over host CPUs → the plan warning names the CPU overcommit by number.

## Tests
`test_fleet_cpu_headroom.py` — 19/19 hermetic (real `app.py` import,
monkeypatched os.cpu_count/run/get_host_stats/load_instances/
load_providers/count_loop_devices/waydroid_image_present/
probe_remote_headroom): local math/warning, fractional cpus, cpus=None
regression, host_cpus=0 fail-open, garbage-cpus never-raises,
missing/string cpus keys, provider isolation, memory+CPU warning
coexistence, remote host_cpus from heartbeat stats + missing-cpu_count
fail-open, fleet_plan passthrough, dashboard contract, no-secret hygiene.
