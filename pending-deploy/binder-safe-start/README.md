# binder-safe-start

## The gap

After a host reboot the binderfs pool (and the loop-device pool) drops back
to the stock set, while containers still bind-mount the specific
`/dev/binderfs/binderN` devices they were created with. The boot-recovery
automation (df-watchdog v14 section 0 on the main node, agent 1.0.32
`sw_boot_recovery` on wing nodes) repairs this in its own cron/thread path
— but the dashboard's Start/Restart buttons and the batch "Start all" /
"Restart all" call `docker start` directly. A start issued before or during
boot recovery (or on a box whose watchdog/selfwatch hasn't run yet) makes
docker auto-create EMPTY DIRECTORIES at the vanished bind sources → dead
binder mounts → a stuck-boot wedge that then burns the stuck-boot circuit
budget into a misleading DEGRADED.

## The fix

- `manager/app.py`: new `_prepare_pools_for_start()` — fast single-`listdir`
  check (a complete pool costs zero subprocesses); on any gap it rmdirs
  empty docker bind-stub dirs (`rmdir` removes only empty dirs, so it can
  never delete data; a non-empty stub is left for human cleanup) and calls
  the existing `ensure_binder_devices()` + `ensure_loop_devices()` (below
  `FLEET_LOOP_DEVICE_FLOOR`). Best-effort, never raises. Called before
  `docker start` in `instance_single_action`'s local start/restart paths,
  guarded by its own try/except at the call site so a helper regression can
  never block a fleet start.
- `wing/agent.py` 1.0.33: new `_sw_prepare_pools_for_start()` — same shape
  over the existing `sw_boot_binder_pool()` (which already handles stub
  dirs) + `sw_ensure_loop_devices()` below `SW_LOOP_DEVICE_FLOOR`.
  Best-effort, never raises. Called in the `/start` endpoint before
  `docker("start", ...)`, also call-site guarded.

The remote manager→agent start path needs no manager change: the agent owns
its own pool. The create path is untouched (triplets are allocated from
existing devices there).

## Tests

`test_binder_safe_start.py` — 25/25 hermetic checks: manager fast path /
missing device / listdir failure / empty + non-empty stub dirs / fail-open
never-raises / loop floor wiring / zero-subprocess fast path; wiring order
(start: prepare → docker start; restart: stop → prepare → start),
fail-open on raising prepare, stop/login/unknown unaffected; agent
VERSION 1.0.33 + ordering, prepare behavior, loop wiring, and the /start
endpoint ordering contract via `inspect.getsource`.

Wired as apply-all preflight t103. 28 existing VERSION-tuple suites widened
to 1.0.33; the wing-boot-recovery exact-version check moved 1.0.32 → 1.0.33;
batch-ops fakes stub `_prepare_pools_for_start` so its exact run-call
assertions stay hermetic.
