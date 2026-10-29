# remote-status-invalidate — optimistic remote-status patch after mutations

## The gap
`fetch_remote_statuses()` (manager/app.py) reuses the last-known-good
per-node snapshot when the fail-fast live fetch fails, so a wedged wing
node doesn't flicker its cards to "stopped" on every dashboard poll. But
the cache was never touched by manager-initiated remote mutations: a
flaky poll right after a dashboard Stop served the PRE-action snapshot
for up to `_REMOTE_STATUS_TTL` (30s) — the card said "running" when the
user had just stopped the instance. After a Start the reverse (card said
"stopped"). Only the local docker-inspect cache
(`invalidate_instance_snapshot`) was invalidated; the remote cache wasn't.

## The fix (manager/app.py only)
- New `patch_remote_status(pid, name, running)`: optimistically sets the
  instance's `running` flag in the cached snapshot. Only that flag is
  touched; other fields stay as last fetched; a missing entry gets a
  minimal `{"name", "running"}` dict. The next successful fetch replaces
  the snapshot wholesale, so the patch can never stick. Never raises.
- New `drop_remote_status(pid, name)`: prunes the entry after a
  successful remote delete. Never raises.
- Wired on success (code == 200) only:
  - `instance_single_action` remote branch: start → True, stop → False,
    restart → True (launch-roblox / roblox-login don't change container
    state and are untouched). Covers the per-instance endpoints AND the
    batch/fleet workers (they share this function).
  - `create_remote_instance` → True (covers single-create and the
    fleet-create worker, which share this function).
  - Remote DELETE route → `drop_remote_status`.

## Deploy
Rides apply-all from HEAD — `manager/app.py` is already in the stage
list; no new stage files. No restart-path change beyond the normal
`systemctl restart jerusalem` that apply-all performs.

## Verify (after the SSH-capable deploy)
1. On a wing node (or with a flaky agent), Stop a remote instance from the
   dashboard: the card flips to "stopped" immediately, even if the next
   poll's agent fetch fails (check the manager log: no stale "running").
2. Start it again: the card flips back to "running" on the same poll.
3. Delete a remote instance: its card disappears and never reappears on
   subsequent failed-fetch polls.
4. `python3 pending-deploy/remote-status-invalidate/test_remote_status_invalidate.py`
   → `PASS=28 FAIL=0`.
