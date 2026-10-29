# wing-start-route-dedup

## The gap

`wing/agent.py` `do_POST` had **two** routes matching
`POST /api/agent/instances/<n>/start`:

1. The dedicated `/start` route (added by binder-safe-start, agent 1.0.33)
   with `_sw_prepare_pools_for_start()` before `docker("start", ...)`.
2. An older `(start|stop)` route whose `start` alternative was **unreachable
   dead code** — Python evaluates the routes in order, so route 1 always
   won. Only its `stop` branch was live.

Not a live bug (the pool prepare always ran), but a maintenance trap in a
security-sensitive handler: the next person to touch the start path could
easily edit the dead branch believing it runs.

## The fix

- The `(start|stop)` route is narrowed to `/stop` only; its body drops the
  `action` conditional and calls `docker("stop", ...)` unconditionally.
- The live `/start` route is byte-untouched (pool prepare preserved).
- `VERSION` 1.0.33 → 1.0.34; all 31 VERSION-tuple suites widened, the two
  exact-version assertions (wing-boot-recovery, binder-safe-start) moved to
  1.0.34 per the bump convention, `remote-autologin/agent.py` snapshot
  refreshed (queue-audit check 4), and the wing-status-cache "M start +
  start|stop + create" message updated.

## Tests

`t106` (`test_wing_start_route_dedup.py`, 9/9, hermetic source inspection):
VERSION is 1.0.34; no `(start|stop)` remains; exactly one `/start` and one
`/stop` route; `/stop` unconditionally stops with no `action` variable;
`/start` still pool-prepares then docker-starts; start/stop/create still
invalidate the status cache (3 calls).
