# wing-missing-script-guard

Wing agent 1.0.28 -> 1.0.29: missing helper-script guard in the node
selfwatch (main-node boot-complete parity).

## The gap

The boot-complete work (2026-10-20 ~14:05 UTC) added missing-helper-script
guards to the main watchdog's sections 3+4: a slow first boot on an image
predating boot-complete.sh finishes booting WITHOUT ever installing the
helper scripts, so restarting them fails silently (`waydroid shell sh
<missing-script>` fails inside Android while `docker exec -d` returns
rc 0). The main watchdog now logs one WARN/day naming the real cause and
skips the restart WITHOUT burning the circuit-breaker budget.

The wing agent's selfwatch had the same hole in check 2 (helper daemons)
and check 3 (stale screencap loop): a dead helper with a missing script
logged a lying "dead -> restart", bumped the recovery count, and burned
the 8/10min helper circuit budget toward a misleading "input-daemon.sh
restarted 9 times in last 600s" DEGRADED — with the root cause (boot setup
never ran) never named. The seventeenth "main had it, wing didn't" parity
item (1.0.5 drift, 1.0.6 adaptive-screencap, 1.0.7 disk/log, 1.0.11
session-death, 1.0.12 loop devices, 1.0.13 memory, 1.0.14 stuck-boot,
1.0.15 keepalive mirror, 1.0.16 binderfs self-heal, 1.0.17 agent HTTP
self-check, 1.0.18 create image preflight, 1.0.19 auth rate-limit, 1.0.20
status cache, 1.0.21 headroom probe, 1.0.22 per-instance degraded, 1.0.23
selfwatch lock, 1.0.24 self-update, 1.0.25 lock race, 1.0.26 route name
gate, 1.0.27 host caps, 1.0.28 proxy-aware rate-limit keys).

## Wing (`wing/agent.py`)

- New `sw_helper_script_path(name, script, vol_base)` — host-side path of
  a helper script inside the instance's data volume (mirrors the volume
  layout check 3 already uses for live.png).
- New `sw_warn_helper_missing(st_dir, name, script, vol_base)` — True when
  the script is missing from the data volume. Logs one WARN per day
  (dated marker file in the selfwatch state dir, stale markers cleaned)
  naming the real cause: "boot setup never installed it; cannot restart
  (recreate the instance from a current image)". Never raises.
- Check 2 (helper daemons): when a helper is dead but its script is
  missing, WARN + skip the restart WITHOUT burning the 8/10min
  circuit-breaker budget and without bumping the recovery count. The
  instance still counts as unhealthy (blocks DEGRADED auto-clear).
- Check 3 (stale screencap): when the stream is stale but
  screencap-loop.sh is missing, distinct WARN, no pkill, no loop restart,
  no stale-loop budget burn.

No manager/dashboard changes: the guard is selfwatch-internal, and the
update badge already shows the agent version via wing-agent-update.

## Tests

`test_wing_missing_script_guard.py` (hermetic — fake docker, tmp
state/volume trees, real agent import), 35/35: exact VERSION 1.0.29 +
manager >=1.0.2 gate, wiring contracts (guard called before the
circuit-breaker in check 2, screencap-loop.sh guarded in check 3), path
units, warn units (present/missing/dated-marker/no-duplicate/stale-marker
cleanup/hostile-st_dir), check-2 integration (missing -> unhealthy, no
docker exec -d, no lying log line, no .ev budget files, no recovery bump;
present -> restart regression), check-3 integration (missing -> no pkill,
no restart, no budget; present -> pkill+restart regression; fresh stream
untouched), main-side parity source intact, never-raises (nonexistent
vol_base, invalid name).
