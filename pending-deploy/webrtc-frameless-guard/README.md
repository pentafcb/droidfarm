# webrtc-frameless-guard

15s frameless guard for the WebRTC path + dashboard post-connect failover.

## The gap
- `stream-frameless-guard` (2026-10-10) gave the MJPEG ws handler and the
  H.264 broadcaster a 15s frameless timeout, but `ScreenVideoTrack.recv()`
  replayed the last frame forever when the frame source died.
- WebRTC is tried FIRST in the viewer chain (WebRTC → H.264/MSE → MJPEG),
  so a wedged pc pinned the viewer on a frozen stream forever — the
  client-side 3s "Imagen congelada" stall badge fired, but nothing ever
  recovered the transport.
- The dashboard's `startWebRTC` only fell back during the 8s startup wait;
  a pc that died after connecting had no fallback at all.

## The fix
- `manager/app.py`: `ScreenVideoTrack(instance_name, provider,
  on_frameless=None)`. A monotonic clock starts at the first `recv()`
  (post-ICE — a slow handshake can't trip it). No fresh frame for
  `WS_FRAMELESS_TIMEOUT` (15s) → fire once: one `[webrtc] <name>: no
  frames for 15s, closing pc` log line, callback invoked (exceptions
  swallowed). `_webrtc_create_offer` passes `_fire_frameless`, which
  schedules the idempotent `_close_pc("webrtc-frameless")` — registry pop,
  adaptive-screencap mark drop, pc close. Never raises.
- `manager/static/index.html`: `pc.onconnectionstatechange` in
  `startWebRTC`. `failed`/`closed` → `webrtcFailover()` (close, null pc/dc,
  `startH264(name)`). `disconnected` → 5s delayed re-check (transient ICE
  hiccups don't thrash the transport). `myPc === pc` guards make manual
  close and the startup-failure path no-ops.

## Tests
`test_webrtc_frameless_guard.py` — 32/32 hermetic (fake aiortc/av, real
asyncio loop, stubbed `_get_webrtc_frame`, shrunk timeout): wiring
contracts, fresh-frames-reset, stall-trips-once, dead-from-first-recv
(incl. the recursive pre-first-frame spin), raising/None callbacks,
full offer→callback→pc-closed integration, dashboard contract.

Preflight: t47 (`32 passed, 0 failed`). Rides apply-all from HEAD.
