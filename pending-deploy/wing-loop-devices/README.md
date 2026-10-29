# wing-loop-devices — wing agent 1.0.12: loop-device guard for wing nodes

Mirrors the main-node loop-device-guard (t66) on the wing side.

## The gap

Binder got the full treatment on wing nodes (installer provisioning +
agent-side checks), but the **second** device pool — loop (~2 per Waydroid
instance at boot; the 2026-10-08 main-node recovery needed ~40 for 20) —
had nothing: `install-wing.sh` never provisioned it, and the agent's
selfwatch never counted it. A rebooted wing node exposes the stock 8 loop
devices (the one-shot provisioning never persisted on devtmpfs), silently
capping the node at ~4 booting instances.

## Changes

**`wing/agent.py` 1.0.11 → 1.0.12**

- `sw_count_loop_devices(loop_dir="/dev")` — name-pattern count of
  `loopN` nodes (excludes `loop-control`, `loopNpM`; best-effort).
- `sw_ensure_loop_devices(st_dir, loop_dir, sys_base)` — when below the
  60-node floor: `modprobe loop max_loop=64` if the module isn't loaded
  (no-op when loaded; `max_loop` can't change on a live module), then
  `mknod` for missing nodes; honest `False` when the live driver's
  `max_loop` reads below floor (mknod can't grow the driver's table).
  Never raises; only internally-generated ints interpolated.
- Selfwatch: runs once per pass (host-level, next to `sw_check_disk()` /
  `sw_trim_container_logs()`). Unfixable pool → `loop.degraded` marker
  (reason on first line) → `selfwatch_state()` degraded map → heartbeat →
  dashboard Nodes "needs attention" badge; relief auto-clears the marker.
- `create_instance_linux()`: self-heals at create time, fails OPEN with a
  WARN in selfwatch.log (loop is consumed at boot, not reserved at
  create). Never blocks creation.

**`wing/install-wing.sh`** (fresh-install script, repo-side)

- Delimited `install_loop_pool()`: persistent `options loop max_loop=64`
  in `/etc/modprobe.d/loop.conf` + `loop` in `/etc/modules` (idempotent),
  immediate modprobe + mknod fallback, count verify with WARNING below 60.
  Called right after the binder setup; progress via `report()`.
  `LP_MODPROBE_D` / `LP_MODULES_FILE` / `LP_DEV_DIR` override real paths
  (hermetic tests).

**Repo-side tooling**

- `deploy-all.sh` preflight syntax gate now also runs
  `bash -n wing/install-wing.sh` (it was the only shipped shell script
  without a gate — the exact class of bug this package's development
  caught: an apostrophe inside the `bash -c '...'` block).

## Tests

- `test_wing_loop_devices.py` — 49 checks: VERSION 1.0.12 + manager gate,
  counter units, healthy no-op (zero run calls), provisioning (modprobe
  only when unloaded, mknod only-missing, recoveries bump), degraded
  lifecycle (marker, single ALERT, no retry spam while degraded, relief),
  max_loop honesty, never-raises, selfwatch wiring, create fail-open,
  `selfwatch_state()` visibility, installer block contracts (delimiters,
  call site, persistent config, 60-floor warning, no apostrophes inside
  the single-quoted block — regression for the real syntax bug caught
  during development), deploy-all gate wiring.
- `test_wing_loop_devices.sh` — 16 checks: verbatim-extracted
  `install_loop_pool()` against fake modprobe/mknod + fake host tree
  (happy path, idempotent re-run, short-pool WARNING, LP_* overrides,
  bash -n, delimiter counts).
- 10 existing suites' VERSION assertions widened to accept 1.0.12 (counts
  unchanged); `remote-autologin/agent.py` snapshot refreshed to HEAD.
