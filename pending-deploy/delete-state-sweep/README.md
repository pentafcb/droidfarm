# delete-state-sweep

## Gap

Instance delete removed the container, its volumes, and the
instances.json entry — but not the per-instance state keyed by
instance name that the dashboard reads. Delete `rbx5`, recreate
`rbx5`, and the new card inherited the dead instance's badges:

- **Login badge**: `roblox_login_results.json` kept the old
  `{ok, ts, reason}` entry, so the new card showed "login OK" for a
  login that never happened on the new instance.
- **Needs-attention badge**: `/tmp/df-watchdog/rbx5.degraded` (and
  the `.ev` circuit-breaker windows, `.helper-missing-*` dated
  files, `.dw-timeout.*` probe markers, `.robloxver`,
  `.sessionstart.ts`) survived, because the watchdog discovers
  instances from instances.json and never cleans state for names
  that disappear. Degraded has priority over the setup badge, so
  the stale badge showed through the whole boot window — the
  watchdog only clears markers once ALL checks go green.
- **Version-drift badge**: `rbx5.roblox_drift` survived the same way.
- **Wing nodes**: the agent's selfwatch dir had the identical
  problem (`<name>.degraded` / `.roblox_drift` / `.roblox_drift_seen`
  / `.sessionstart.ts` / `.ev` files), surfacing as a stale "needs
  attention" badge in the Nodes modal.
- Also swept: the manager's in-memory docker-stats cache entry and
  the shared H.264 broadcaster for the instance (otherwise a
  connected viewer kept pulling frames for a dead container until
  the frameless guard fired).

## Fix

- `manager/app.py`: `drop_login_result()` (atomic 600 save, same
  sanitization as `record_login_result`), `drop_watchdog_state()`
  (exact files + a `name + "."` prefix scan for `.ev` /
  `.helper-missing-` / `.dw-timeout.` files), `drop_resource_cache()`,
  `_stop_h264_broadcaster()` (pop under lock, stop outside it).
  Wired into both delete paths: local (all four) and remote-200
  (login result + broadcaster; the watchdog state lives on the node
  and is swept by the agent's delete).
- `wing/agent.py`: `drop_selfwatch_state()` (same shape, reusing
  the auto-clear scan pattern), wired into `delete_instance_linux()`.

## Deliberately NOT swept

- **Saved accounts** (`roblox_accounts.json`): credentials are never
  deleted by the delete path. A recreated instance keeps its saved
  account; only the login *outcome* is dropped.
- **recoveries.json** (agent): a node-wide total, not per-instance.
- **Watchdog/event logs**: append-only history, not state.
- **Global watchdog/selfwatch files**: an instance literally named
  `docker`, `disk`, `mem`, or `log` shares filenames with global
  markers (`docker.degraded`, `disk.prune.ev`, …) that are
  indistinguishable on disk, so both sweeps carry an explicit
  global-filename exclusion set and leave those files alone.

## Tests

`test_delete_state_sweep.py`, 45/45 hermetic (t134): login-result
drop/missing/invalid/corrupt/600, watchdog full file set, `rbx1`
vs `rbx10` prefix guard, global-file guard (including the
`docker`-named instance), missing dir, resource-cache drop, H.264
broadcaster pop + raising-stop tolerance, do_DELETE wiring for
both paths, agent selfwatch full set + guards, delete wiring, and
a `selfwatch_state()` cross-check proving the sweep clears exactly
what the heartbeat reports.
