# fleet-cpu-headroom

CPU overcommit warning for the Create-fleet plan preview.

`manager/app.py` `fleet_headroom()` gains `host_cpus` / `current_cpus` /
`new_cpus` / `cpu_overcommit` fields and a plan-time warning when CPU
limits would exceed host CPUs — for both the local node (via
`os.cpu_count()`) and wing nodes (via the agent's heartbeat `cpu_count`,
no agent change). Warning only, never a refusal: Docker `--cpus` is a
quota, not a reservation. `fleet_plan()` passes the validated per-instance
`cpus` through; legacy 4-arg calls keep the old behavior (no CPU warning).

Preflight t107: `test_fleet_cpu_headroom.py` — PASS=19 FAIL=0.
Rides apply-all from HEAD (manager/app.py already staged).
