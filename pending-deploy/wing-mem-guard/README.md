# wing-mem-guard — wing agent 1.0.13: memory-pressure guard + OOM surfacing

## The gap
The main-node watchdog learned to watch host memory in v7 (2026-10-08):
`MemAvailable <1024MB` deduped into a `mem.degraded` alert, plus fresh
"Out of memory" dmesg lines surfaced as WARNs. Remote (wing) nodes never got
that check. An OOM-pressure wing node had its containers killed while the
agent's selfwatch just restarted them — burning the container-start circuit
budget (>6/hour) until a misleading `wd-<name> restarted 7 times` DEGRADED,
with the real cause (memory pressure) never surfaced. Picking the OOM victim
stays human; the node just needs to say so.

## What it does
`sw_check_memory()` runs as a host-level guard in `selfwatch_loop()` (once
per 60s pass, not per instance; Linux only), alert-only — nothing is ever
stopped, killed, or restarted by it:

- **Low-memory path**: reads `MemAvailable` from `/proc/meminfo`
  (`SW_PROC_MEMINFO`-overridable). Below `SW_MEM_LOW_KB` (1048576, v7
  parity) counts an event in the `SW_MEM_LOW_WINDOW` (3600s) sliding window;
  `SW_MEM_LOW_TRIPS` (3) such readings -> `mem.degraded` marker (reason on
  the first line, same convention as `disk.degraded`/`loop.degraded`) and a
  single `memory DEGRADED` log line. Relief (>= floor) removes the marker +
  event file and logs `memory pressure relieved`. Silent while degraded
  (v7 parity: no repeat spam). Unreadable meminfo -> silent skip.
- **OOM path**: `sw_oom_count()` counts "Out of memory" lines
  (case-insensitive, the main-node `grep -ci` idiom) in dmesg; fresh kills
  since the `oom.count` cursor -> one WARN per pass
  (`WARN OOM killer fired Nx since last check (report only - an instance
  may be dead, check docker ps)`). The cursor always advances; dmesg
  failures skip silently.
- **Visibility**: `mem.degraded` flows through the existing
  `selfwatch_state()` degraded map -> heartbeat -> the dashboard Nodes
  modal "needs attention" badge, and the alerts land in `selfwatch.log` so
  the Eventos modal tags them (canonical severity tagging) with zero
  manager/dashboard changes.

All helpers best-effort, never raise. `wing/agent.py` VERSION goes
1.0.12 -> 1.0.13; the manager's `>=1.0.2` gates are unaffected.

## Tests
`test_wing_mem_guard.py` — 36 hermetic checks: version bump + gate compat,
`sw_read_mem_kb` / `sw_oom_count` units, healthy no-op, low-reading
warn-then-degraded lifecycle (E: 1/3, 2/3, 3/3 marker + reason + single log
line), silence while degraded, relief cleanup, unreadable-meminfo no-op,
OOM cursor (delta WARN, no repeat, later delta, low-pass scan), never-raises
(corrupt state dir, raising internals), selfwatch_loop wiring contract
(host-level, once per pass, try/except), degraded-map visibility + 160-char
truncation, and the v7-parity constants.
