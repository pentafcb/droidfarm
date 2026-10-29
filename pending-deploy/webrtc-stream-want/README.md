# webrtc-stream-want

Hooks the WebRTC peer-connection path into the adaptive-screencap viewer
registry (`stream_wanted` marker).

## Why

The in-container screencap loop captures fast (0.12s) only while
`stream_wanted` is fresh, slow (2s) otherwise. MJPEG ws and H.264 ws
viewers mark it — but WebRTC's `ScreenVideoTrack` reads `live.png`
directly, bypassing both handlers. A WebRTC-only viewer would get
2s-interval frames after adaptive-screencap deploys.

## The fix (`manager/app.py` only)

- `stream_viewer_open(provider, instance_name)` after the answer SDP is
  set — a failed handshake (`setRemoteDescription` raising) never leaks a
  mark.
- The `connectionstatechange` teardown is now an idempotent `_close_pc()`
  (one close per pc): pops `_webrtc_pcs`, calls
  `stream_viewer_close`, closes the pc, never raises.
- A 180s stale-handshake sweep (`loop.call_later`) closes pcs stuck in
  `new`/`connecting` — an abandoned handshake (tab killed mid-ICE) fires
  no state change and would otherwise pin the fast capture loop forever.
- Remote providers are a no-op (agent 1.0.6 maintains its own marker from
  screenshot-fetch traffic); the existing 15s `stream_want_retouch()`
  keeps long-lived WebRTC markers fresh.

## Tests

`test_webrtc_stream_want.py` — 40/40 hermetic (fake aiortc/av, real
asyncio loop, `_STREAM_WANT_BASE` redirected to tmp): wiring contract,
happy path, idempotent close, remote no-op, handshake failure, stale
sweep (stuck vs connected vs post-close), raising `pc.close`,
retouch integration, multi-viewer refcounting.
