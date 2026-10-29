# stream-frameless-guard

Server-side frameless guards on both streaming loops: they now close
themselves after 15s with zero frames instead of spinning forever.

## Problem

A dead frame source (dead in-container screencap loop, wedged Waydroid
session, unreachable wing node) made the streaming loops spin at 20Hz
forever:

- `manager/app.py::_handle_ws` (MJPEG): one thread per viewer. For
  remote instances every tick is an HTTP fetch against the wing agent —
  up to 20 failed requests/sec per viewer on a fast-failing node, or
  10s-blocked iterations (wing_request retries 3x) on a hung node.
- `manager/h264_stream.py::H264Broadcaster._run`: 20Hz
  `frame_source()` calls (HTTP fetch + JPEG decode per tick for remote
  instances) with an idling ffmpeg encoder attached. The loop previously
  only stopped on the subscriber-idle timeout, so a subscribed viewer
  with dead frames burned CPU and hammered the node indefinitely.

## Change

- `manager/h264_stream.py`: `H264Broadcaster` gains
  `frameless_timeout=15.0`. `_run` breaks when no frame arrives for that
  long; the existing `_cleanup()` closes subscribers and reaps the
  encoder. The dashboard's H264 `ws.onclose` already falls back to MJPEG
  (`h264Fail`), and a later viewer re-creates the broadcaster via the
  registry.
- `manager/app.py`: new `WS_FRAMELESS_TIMEOUT = 15` module constant.
  `_handle_ws` tracks the last delivered frame and breaks the loop after
  15s frameless, logging one `[ws] mjpeg <name>: no frames for 15s`
  line. The dashboard's MJPEG `onclose` reconnects after 3s, so recovery
  is automatic once frames return.

The 15s threshold sits between the client-side stall badge (3s, shows
first so the UX stays honest) and the watchdog's stale-stream restart
(30s, which repairs the underlying cause).

## Tests

24/24 pass (`test_stream_frameless_guard.py`, hermetic — stub encoder,
faked sockets, no ffmpeg/docker/network): broadcaster trip closes
subscribers + reaps the encoder, no trip while frames flow, clock
resets on intermittent frames, malformed frames still trip, 15s
defaults; MJPEG loop exits + logs once + closes the socket on persistent
frameless, no trip while frames flow, client close still ends a healthy
loop, trip after intermittent frames stop. Regression: h264 59/59 still
green.

## Deploy

Rides apply-all from HEAD (`manager/app.py` + `manager/h264_stream.py`
already in the stage list). Suite `t30` in the preflight. Standalone scp
+ `systemctl restart jerusalem` in DEPLOY.md (app.py change needs the
restart; h264_stream.py is imported by app.py). Blocked in worker
sessions: no VPS SSH (one test per session, no retries).
