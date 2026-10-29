# wing-failfast — fail-fast wing node requests on hot paths

## What it fixes

`wing_request()` retried every failed connection 3x with 1s sleeps,
hard-coded. On the four hot paths that hit a wing node at high frequency,
a wedged node turned into a thread-pool attack on the manager:

- `GET /api/instances` remote fetch (dashboard polls every 10s): up to
  ~17s serial block per provider (5s timeout x3 + 2x1s sleep), holding a
  `BoundedHTTPServer` handler thread for every poll.
- MJPEG `_handle_ws` per-frame screenshot fetch: up to ~32s per frame
  (10s x3), delaying the 15s frameless guard and pinning a handler
  thread per viewer.
- H.264 broadcaster `frame_source` tick: up to ~32s per tick, delaying
  the broadcaster's frameless guard.
- WebRTC `_get_webrtc_frame`: up to ~32s per frame.

With the 64-thread cap from http-server-bounds, a hung wing node plus a
couple of viewers could exhaust the pool and 503 legitimate users.

## Changes (manager/app.py only, rides apply-all from HEAD)

1. `wing_request(..., attempts=3)`: retry budget is now a parameter
   (default 3 = historical behavior, clamped to >= 1).
2. The four hot paths pass `attempts=1`. Retry policy lives at the loop
   level where it already exists: 10s poll cadence, 3s MJPEG reconnect,
   H.264 re-subscribe / broadcaster re-create, WebRTC recv loop.
3. User-initiated one-shots (input, launch-roblox, start/stop/restart,
   create, delete, login) keep the default 3 attempts, so a single
   transient reset doesn't fail the action.
4. NEW `fetch_remote_statuses()`: the `/api/instances` poll path fetches
   with `attempts=1` and, on failure, reuses the last-known-good snapshot
   for 30s (`_REMOTE_STATUS_TTL`) instead of flickering every remote card
   to "stopped". Best-effort, never raises; corrupt payloads filtered.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (app.py is
already in the stage list). Standalone:

```bash
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'systemctl restart jerusalem && sleep 2 && systemctl is-active jerusalem'
```

Verify: dashboard grid refreshes stay fast with a wing node down;
`srt` cards keep last-known state for ~30s instead of flipping to
"stopped" on one failed poll; stream viewers recover via the existing
frameless guards.

## Tests

`test_wing_failfast.py`: 43/43 hermetic (fake urllib: attempts
boundaries/clamping, retry-recovery, HTTPError passthrough, no-retry on
generic errors; cache populate/TTL/boundary/expiry/isolation/garbage;
source contracts for all four hot paths + user one-shots untouched).
Preflight t43.
