# wing-stuck-boot — wing agent 1.0.14: stuck-boot detection + auto-restart

## The gap
The main-node watchdog restarts a container whose Android never finishes
booting (up >15min, no `booted` marker) since v3 (2026-10-06). Wing agents
had no equivalent: on a wing node a stuck boot was invisible. The drift
check (1b) and the session-death check (1c) both skip never-booted
instances, so only the helper checks fired — restarting dead
`screencap-loop.sh`/`input-daemon.sh`/`roblox-keepalive.sh` daemons that
can never work without a live Waydroid session, burning the helper circuit
budget (>8/10min) until a misleading `"<script> restarted 9 times"`
DEGRADED, with the root cause (dead boot) never repaired.

## What it does
`sw_check_stuck_boot()` runs as selfwatch check 1d (every 60s, Linux only),
owning the first-boot case; check 1c keeps owning the booted-once case:

- **Probe**: `docker inspect -f '{{.State.StartedAt}}' wd-<name>` parsed by
  `_sw_parse_started_at()` (RFC3339: `Z`, `+HH:MM`/`-HHMM` offsets,
  fractional seconds; garbage -> None, never raises).
- **Skip**: booted marker present (strike counter reset) or invalid name.
- **Stuck**: no marker yet -> unhealthy pass, but the helper/stream checks
  still run (main parity). An ambiguous uptime probe (inspect rc!=0,
  garbage StartedAt, raising docker) also yields "stuck" — the main node
  sets `healthy=0` on a missing marker regardless of probe success — but
  writes NO strike and fires NO restart (the restart is fail-open on
  ambiguous data).
- **Strike lifecycle**: container provably up >900s -> strike N; 2nd
  strike -> `_sw_cb_guard` (6/hour "container" budget) -> `docker restart`.
  Success logs `<name> stuck booting (no marker after 15m) -> docker
  restart (N/6 this hour)`, bumps recoveries, clears strikes, and returns
  "restarted" (early return — the container is fresh). Failure logs
  `stuck-boot restart FAILED` and keeps strikes for the next minute.
- **Circuit budget**: >6 restarts/hour -> instance DEGRADED (reason on the
  marker's first line) -> existing selfwatch_state -> heartbeat ->
  dashboard Nodes "needs attention" badge; silent while degraded;
  recovery auto-clears via the existing step-4 path.
- All helpers best-effort, never raise. `wing/agent.py` VERSION goes
  1.0.13 -> 1.0.14; the manager's `>=1.0.2` gates are unaffected.

## Test contract note (2026-10-16)
The first version of this suite asserted "skip" on ambiguous probes. That
was wrong: the main-node watchdog marks the pass unhealthy whenever the
marker is absent, independent of the probe. The implementation was fixed
only in its docstring; the tests (C4/C5/C6/G3, B2's colon-less `-HHMM`
input) were corrected to the main-parity contract.

## Tests
26/26 hermetic (`test_wing_stuck_boot.py`): exact VERSION 1.0.14 +
manager-gate compat, StartedAt parser units, skip paths, strike lifecycle,
circuit budget, 1d wiring (restarted -> early return, helpers untouched),
never-raises. Preflight t69.
