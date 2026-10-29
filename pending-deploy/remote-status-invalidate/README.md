# remote-status-invalidate

Optimistic remote-status patching for the manager's last-known-good
wing-node snapshot cache.

## Why
The dashboard polls `/api/instances` every 10s. `fetch_remote_statuses()`
fails fast (`timeout=5, attempts=1`) and, on failure, serves the cached
snapshot so a wedged node doesn't flicker its cards. That cache was never
invalidated by mutations the manager itself initiated — a flaky poll right
after a Stop showed "running" for up to 30s, the exact action the user had
just taken, with no way to tell it was stale.

## What
- `patch_remote_status(pid, name, running)` / `drop_remote_status(pid, name)`
  in `manager/app.py` (both best-effort, never raise).
- Wired into the remote branches of `instance_single_action`
  (start/stop/restart, success only), `create_remote_instance` (success
  only), and the remote DELETE route (success only). Local instances are
  untouched (their docker-inspect cache already invalidates per action).
- No protocol, API, agent, or dashboard change: the optimistic flag flows
  through the existing `remote_status` dict into the existing card render.

## Tests
`test_remote_status_invalidate.py` — 28 checks, hermetic (real app import,
faked `wing_request`/`load_providers`): helper units (create/patch/drop,
key parity with the `(pid, name)` lookup, garbage never raises), wiring
source contracts, and functional drives of the real fallback path
(stale-serve baseline → patched value → successful fetch overwrites).

## Preflight
t105, exact-count grep `PASS=28 FAIL=0`.
