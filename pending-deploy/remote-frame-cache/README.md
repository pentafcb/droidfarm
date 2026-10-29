# remote-frame-cache

One wing screenshot fetch per remote instance per 100ms, shared by every
viewer and every streaming transport.

## The gap
The local screenshot path was already mtime-cached inside
`capture_screenshot()`, but the remote path had no cache at all. Three
call sites each did their own HTTP GET to
`/api/agent/instances/{name}/screenshot` per frame:
- MJPEG `_handle_ws` — per viewer thread (20Hz each)
- WebRTC `ScreenVideoTrack.recv` via `_get_webrtc_frame` — per viewer
- H.264 broadcaster `frame_source` — per tick (shared across H.264
  viewers, but its own fetch)

Two viewers on one `srt` instance = 40 fetches/sec against the wing node
for the same ~8Hz framebuffer.

## The fix
`get_remote_screenshot(wing, prov_id, name)` in `manager/app.py`:
- TTL 100ms cache keyed by `(prov_id, name)`; concurrent viewers share
  the cached bytes (identical object, read-only downstream).
- attempts=1 fail-fast inside (the frameless guards own retry at loop
  level, same as before).
- Failure -> entry dropped, None returned. Never serves stale: a dead
  source must read as None so the 15s frameless guards trip.
- Stale entries (>60s untouched) pruned on write — a deleted instance's
  JPEG doesn't linger.
- Best-effort, never raises.

TTL rationale: the in-container capture loop runs at ~8Hz when a viewer
is attached, so 100ms staleness is imperceptible; fetch traffic drops
from N x 20Hz to <= 10Hz per instance. The agent-side `stream_wanted`
marker is still re-touched on every served screenshot (10Hz >> the 90s
sweep), so adaptive-screencap behavior is unchanged.

## Tests
`test_remote_frame_cache.py` — 31 hermetic checks: hit/TTL/miss,
failure-drop (no stale serve), short-payload rejection, raising/never-
raises, garbage wing dicts, per-key isolation, stale-entry prune,
10-thread x 30-call sharing (fetch count <= thread count), all three
call-site source contracts, and a functional drive of `_get_webrtc_frame`'s
remote branch through the cache with a real JPEG.
