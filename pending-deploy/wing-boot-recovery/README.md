# wing-boot-recovery

Counterpart to the main node's **boot-recovery** (df-watchdog.sh v14) for wing nodes.
Closes the post-host-reboot gap in the wing agent's selfwatch: a reboot stops
every `wd-*` container (docker run sets no `--restart` policy) and drops the
binderfs pool back to the stock set (only the CREATE path ever restored it,
agent 1.0.16). The selfwatch container-down check then fired for EVERY
stopped instance in the same pass — a Waydroid boot storm on the wing box,
and `docker start` on containers whose bind-mounted `/dev/binderfs/binderN`
sources had vanished (docker auto-creates empty dirs at missing bind sources
→ dead binder mounts → stuck-boot wedge → 6/hour container-start circuit
budget burned into mass DEGRADED).

## What changed (agent 1.0.32, `wing/agent.py`)

- `sw_boot_recovery()` — new section 0, runs FIRST in every held selfwatch
  pass (before the per-instance checks): on a fresh boot (no
  `/tmp/wing-boot.done` sentinel + host uptime < 900s) it (1) restores the
  binderfs pool (mount if needed + `binder-control` writes; empty docker
  stub dirs are `rmdir`'d — empty-only, can never delete data — and
  recreated; non-empty stubs left alone with one ALERT per boot and the
  fleet start paused), (2) stagger-starts stopped instances (3 `docker
  start`s per 60s pass, sorted order, `.degraded` skipped, 3 attempts per
  instance before DEGRADED with the reason on the marker's first line →
  `selfwatch_state()` degraded map → heartbeat → dashboard Nodes
  "needs attention" badge, zero manager/dashboard changes), (3) writes the
  sentinel when the queue drains.
- `selfwatch_check_instance()` — new `boot_recovery` param: while section 0
  is active (`progress`/`incomplete`/`pool-paused`) its container-down
  branch defers its own starts (no race, no herd). `None` (direct callers)
  preserves the old behavior exactly.
- `sw_boot_recovery_active()` — test seam: sentinel missing + uptime under
  the window. Unreadable uptime fails open to False.
- Env seams (garbage degrades to defaults): `DF_BOOT_WINDOW_S` (900),
  `DF_BOOT_MAX_STARTS` (3), `DF_BOOT_ATTEMPTS` (3), `DF_BINDER_COUNT` (64),
  `DF_PROC_UPTIME`, `DF_BINDERFS`, `DF_BOOT_TMP` (sentinel dir, /tmp default).
- Missed window (uptime ≥ 900s): sentinel written once, normal per-instance
  checks own recovery (today's behavior). Never raises; never deletes
  instance data, credentials, or tokens; never stops/kills/restarts anything
  except `docker start` on stopped containers.

## Tests

`test_wing_boot_recovery.py` — 85/85 hermetic checks (fake docker/shell/
binderfs/uptime/sentinel): fresh-boot detection, sentinel no-op, missed
window, staggered starts (3/pass, sorted, sentinel on drain), failed-start
retry, attempt budget → DEGRADED (reason on first line, surfaces via
`selfwatch_state()`), pre-degraded skip, pool restore (exactly the missing
names, no metachars), empty-stub rmdir + recreate, non-empty stub → one
ALERT per boot + fleet start paused, incomplete pool, docker ps failure,
check_instance deferral + old-behavior regression, mount path, env
sanitize, invalid-name filtering, never-raises, pass_once wiring.

## Deploy

No new stage files — `wing/agent.py` is already in the apply-all stage list;
rides apply-all from HEAD. The agent self-updates from the manager (1.0.24
mechanism), so wing nodes pick up 1.0.32 on their next heartbeat after the
manager deploy — no installer re-run needed.
