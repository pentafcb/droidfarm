# resize-cache-invalidate (agent 1.0.36)

## The gap

`wing-status-cache` (agent 1.0.20) added a 10s-TTL per-instance status
snapshot on wing nodes with the documented contract: "Mutation endpoints
(create/start/stop/delete) invalidate the entry, so the next poll shows
fresh state."

The `/resize` route (agent 1.0.31, live RAM/CPU resize via
`docker update`) was added two versions later and never got the
invalidation. After a remote resize, the agent kept serving the pre-resize
snapshot until TTL expiry.

In practice the snapshot holds usage/setup/degraded/drift — not the limits
themselves (those live in the manager's `instances.json`, updated on a
200) — so this is a contract-consistency fix: every mutation route now
invalidates, no exceptions.

## The fix

`wing/agent.py` 1.0.35 → 1.0.36: the `/resize` do_POST route calls
`invalidate_agent_status_cache(name)` after a successful
`resize_instance_linux()` (gated on `ok`, the create-route pattern —
validation 400s return before the mutation, like the other routes).

## Tests

`test_resize_cache_invalidate.py`, 18/18:

- A. VERSION exactly 1.0.36 + parse ordering + manager >=1.0.2 gate
- B. source contract: resize block invalidates, gated on `ok` after the
  mutation call
- C. validation-failure paths (400s) return before any invalidation
- D. functional: seeded cache → invalidate → entry dropped → next call
  refetches via docker
- E. regression: create/start/stop/delete routes still invalidate
- F. `invalidate_agent_status_cache` never raises on garbage names

## Wiring

- Preflight t110 (`"PASS=18 FAIL=0"`), deploy-all.sh header + green-log +
  APPLY.md table row + suite lists (110 suites).
- 31 VERSION-tuple suites widened to 1.0.36; the exact-bump assertions in
  wing-install-guard / wing-boot-recovery / binder-safe-start widened to
  1.0.35+ (exact 1.0.36 owned here).
- remote-autologin agent.py snapshot refreshed to HEAD (audit check 4).
- No new stage files — `wing/agent.py` already in the apply-all stage
  list; rides it from HEAD. Vultr nodes self-update on the next heartbeat.
