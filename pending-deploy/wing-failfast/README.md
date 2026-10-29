# wing-failfast

Fail-fast `wing_request` on the hot paths + a last-known-good remote-status
cache for the dashboard poll.

## The problem

`wing_request()` retried every failed connection 3 times with 1s sleeps —
hard-coded, no way to opt out. That policy is right for user-initiated
one-shots (a tap, a launch, a create), but on the four hot paths that hit a
wing node at high frequency it turned one wedged node into a thread-pool
attack on the manager:

| Hot path | Old worst case per call | Who pays |
|---|---|---|
| `GET /api/instances` remote fetch (every dashboard poll, 10s) | ~17s serial per provider (5s x3 + 2x1s) | one BoundedHTTPServer handler thread per poll |
| MJPEG `_handle_ws` per-frame screenshot | ~32s per frame (10s x3) | one handler thread per viewer; delays the 15s frameless guard |
| H.264 broadcaster `frame_source` tick | ~32s per tick | the shared broadcaster thread; delays its frameless guard |
| WebRTC `_get_webrtc_frame` | ~32s per frame | aiortc executor thread |

After http-server-bounds capped the pool at 64 threads, a hung wing node
plus a couple of open viewers could exhaust it and 503 legitimate users.

## The fix

- `wing_request(..., attempts=3)`: the retry budget is a parameter now
  (default = historical behavior; clamped to >= 1; garbage -> 3).
- The four hot paths pass `attempts=1`. Retries move to the loop level,
  where they already exist: 10s poll cadence, 3s MJPEG reconnect, H.264
  re-subscribe / broadcaster re-create, WebRTC recv loop. Per-call retries
  there only added latency and pinned threads.
- User-initiated one-shots (input, launch-roblox, start/stop/restart,
  create, delete, login) keep the default 3 attempts.
- NEW `fetch_remote_statuses()`: the `/api/instances` poll fetches with
  `attempts=1`, and on failure reuses the last-known-good per-node
  snapshot for 30s instead of flickering every remote card to "stopped".
  Beyond 30s (or never seen) the node contributes nothing — cards read
  "stopped", honestly. All helpers best-effort, never raise; corrupt
  payloads are filtered, garbage provider records skipped.

## Why the cache, not just attempts=1

Fail-fast alone would make one dropped packet flip every `srt` card to
"stopped" for a 10s poll cycle — a visible flicker with zero information.
The 30s cache keeps the last-known state through transient blips (the
dashboard already polls every 10s, so 30s = ~3 missed polls before the
cards admit they don't know). Same "no data, never lies" convention as
the instance-health-cache and the remote resource TTL.

## Tests

`test_wing_failfast.py` — 43/43 hermetic, no docker/network:
- attempts boundaries (1/2/3, 0/-5 clamp, garbage -> 3), retry recovers
  on 2nd attempt, HTTPError passthrough with no retry, generic exceptions
  fail immediately (documents existing behavior), JSON vs raw-bytes
  responses;
- cache: populate, stale-`last_seen` skip, failure-within-TTL reuse,
  expiry, TTL boundary, fresh-empty beats stale, per-provider isolation,
  garbage providers/payloads, raising transport never propagates,
  `last_seen` boundaries;
- source contracts: signature default, all four hot paths carry
  `attempts=1`, `/api/instances` uses the helper, user one-shots
  (launch/input/delete/stop) carry no override, TTL constant.

Preflight t43, exact-count grep "43 passed, 0 failed".
