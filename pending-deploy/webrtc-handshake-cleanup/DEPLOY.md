# DEPLOY — webrtc-handshake-cleanup

## What
- Server only (`manager/app.py::_webrtc_create_offer`): the handshake
  (SDP parse → `setRemoteDescription` → `createAnswer` →
  `setLocalDescription`) is wrapped in try/except. On failure the already
  idempotent `_close_pc("handshake-failed")` runs immediately — registry
  pop + `pc.close()` — then the exception re-raises so
  `webrtc_offer_sync` still returns `None` and the dashboard falls back
  to H.264/MJPEG as before.

## Why
A failed handshake (garbage SDP, failed answer) previously left the
`RTCPeerConnection` (threads, UDP sockets) and its `_webrtc_pcs` registry
entry alive until the 180s stale-handshake sweep reaped them — up to 3
minutes of leaked resources per failed offer. The offer route is
auth-gated, but a buggy client retry loop could accumulate them. The
viewer mark is untouched by the failure path: it opens only after
`setLocalDescription` succeeds, and `stream_viewer_close` clamps at 0.

## Deploy
Rides apply-all from HEAD (`manager/app.py` is already in the stage
list — no new stage files).

Standalone (SSH-capable session, root on the target box):
```
scp manager/app.py root@<host>:/root/jerusalem/app.py
ssh root@<host> 'systemctl restart jerusalem'
```

## Verify
1. `systemctl is-active jerusalem` → `active`
2. Dashboard loads (HTTP 200); open a viewer — WebRTC connects normally
   (no behavior change on the happy path).
3. Post a garbage SDP to `/api/instances/<n>/webrtc/offer` (authed) →
   HTTP 500 `{"error": "webrtc failed"}` and the dashboard falls back to
   H.264/MJPEG; no pc/thread accumulation on repeated attempts.
