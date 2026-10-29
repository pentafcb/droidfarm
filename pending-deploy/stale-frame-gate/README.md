# stale-frame-gate

## The bug

A dead in-container screencap loop leaves `live.png` behind, and both
screenshot getters returned that frozen frame forever: the cache key
is the file mtime, and a dead loop never changes it. Every downstream
guard therefore saw a truthy frame on every tick and could never fire:

- MJPEG (`_handle_ws`): truthy frame → `last_frame` reset + 1s ping →
  the 15s frameless guard never tripped, the dashboard's 3s stall badge
  never showed.
- H.264 broadcaster: truthy frame → heartbeat encodes kept flowing →
  its 15s frameless guard never tripped.
- WebRTC track: truthy frame → `_last_fresh` kept updating → its 15s
  frameless failover never fired.

The viewer showed a frozen frame labelled live until the watchdog's
30s stale-stream restart repaired the loop — and even then, nothing
had told the viewer anything was wrong.

## The fix

`capture_screenshot()` (manager, local instances) and
`screenshot_bytes_linux()` (wing agent 1.0.40, remote instances)
return `None` when `live.png` is older than `SCREENSHOT_STALE_SECS`
(10s, both sides). 10s is above the healthy loop cadence (0.12s fast
with a viewer attached, 2s idle, worst cycle ~7s with a 5s screencap
timeout) and below the 15s frameless guards and the 30s watchdog
stale check. No other code changes: the MJPEG, H.264, and WebRTC
loops already treat `None` as "no frame" and close/fail over, the
agent screenshot route already 404s on `None`, and the manager's
remote cache already drops on a failed agent fetch.

## What it is not

- Not a watchdog change: the 30s stale-stream restart still repairs
  the dead loop; this only makes the viewer honest in the meantime.
- Not a screencap-loop change: the loop's cadence and signalling are
  untouched.
- Not a threshold tweak for static screens: a static screen still
  rewrites `live.png` every cycle, so its mtime stays fresh; only a
  genuinely dead loop goes stale.

## Tests

`python3 test_stale_frame_gate.py` → 35/35 (threshold constants +
agent 1.0.40 bump, gate placement before the cache-hit return,
downstream propagation contracts, mocked-filesystem behaviour for
both getters: fresh/boundary/stale/future/missing, cache-hit masking
proof, no file read on stale frames).
