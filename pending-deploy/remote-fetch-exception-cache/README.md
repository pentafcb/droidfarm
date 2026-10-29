# remote-fetch-exception-cache

The asymmetry the node-fetch-badge package left behind: its suite
asserted the failure counter on the `wing_request`-raise path, never
the returned mapping — so nobody noticed the raise path skipped the
last-known-good cache entirely.

`fetch_remote_statuses()` has two failure branches:

- non-200 / non-dict body: counter++, then serve the cached snapshot
  while it is younger than `_REMOTE_STATUS_TTL` (30s).
- `wing_request` raises (timeout, connection error — the most common
  failure mode for a wedged or slow agent): counter++, `continue` —
  the cache was never consulted.

So a single transient timeout flipped every card on that node to
"stopped" at once (GET `/api/instances` reads `remote_status.get(...)`
or `{}` → `running` False), while the node-fetch badge needs 6
consecutive failures (~60s at the 10s poll cadence) before it says
anything. The function's own docstring already promised cache reuse
"on a fetch failure" — the raise path violated it.

## What changed

- `manager/app.py` only: the `except Exception` branch of
  `fetch_remote_statuses()` now does the same cache lookup as the
  non-200 branch (same TTL check, same non-dict guard) before
  continuing. Counter behavior is unchanged: a raise still counts
  toward the 6-failure badge threshold, and a stale or missing cache
  still contributes nothing.

## Deliberate boundaries

- No threshold change: the badge still trips at 6 consecutive
  failures. The fix only makes the first 30s of those failures honest
  (last-known state) instead of misleading ("stopped").
- No cache-TTL change: 30s, same as the non-200 branch. A wedged agent
  still degrades to "stopped" + badge after the cache expires.
- No dashboard change: the cards already render whatever
  `/api/instances` returns; the fix is entirely in what the manager
  returns for a raising node.
- No agent change, no VERSION bump: the agent's behavior is untouched;
  this is a manager-side fetch-policy fix.
- No new state: the cache already existed; the fix only reads it on
  one more path. Nothing new to sweep on delete.

## Tests

- `test_remote_fetch_exception_cache.py` (27/27, hermetic): real
  `fetch_remote_statuses()` against a fake `wing_request` — raise
  after success serves the cache (twice), non-200 behavior unchanged,
  success resets the counter and replaces the cache, fresh-aged
  (TTL−1) vs stale (TTL+1) cache boundaries, no-cache and non-dict
  cache empties, stale-heartbeat skip, non-dict provider skip,
  two-provider mix (raising + ok), non-`ConnectionError` raise, and
  never-raises inputs, plus source contracts (3 `out.update(per)`
  sites, fix comment, docstring promise).
- Regression proof: with the fix reverted, the suite fails 7/27
  (all raise-path cache checks + both source contracts).
- Regressions green: node-fetch-badge 62/62 (its raise-path counter
  assertions are untouched by the fix).
