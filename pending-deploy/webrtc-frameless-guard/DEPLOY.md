# DEPLOY — webrtc-frameless-guard

## What
- Server: `ScreenVideoTrack` (manager/app.py) fires `on_frameless` once when
  no fresh frame arrives for `WS_FRAMELESS_TIMEOUT` (15s, same as the
  MJPEG/H.264 guards); `_webrtc_create_offer` schedules the idempotent
  `_close_pc("webrtc-frameless")`, which pops the pc registry, drops the
  adaptive-screencap viewer mark, and closes the pc.
- Dashboard: `startWebRTC` registers `pc.onconnectionstatechange` —
  `failed`/`closed` fail over immediately to H.264 (which falls back to
  MJPEG); `disconnected` gets a 5s grace for transient ICE hiccups.
  All paths guarded by `myPc === pc` so manual viewer close and the
  startup-failure path never double-fallback.

## Why
A dead frame source (dead screencap loop, wedged Waydroid session,
unreachable wing node) made the WebRTC track replay the last frame
forever. WebRTC is first in the viewer chain, so the browser stayed
frozen instead of failing over to H.264/MJPEG. The dashboard previously
had no post-connect failure handling at all.

## Deploy
Rides apply-all from HEAD (manager/app.py + manager/static/index.html
are already in the stage list — no new stage files).

Standalone (SSH-capable session, root on 209.222.10.93):
```
scp manager/app.py manager/static/index.html root@209.222.10.93:/root/jerusalem/
scp manager/static/index.html root@209.222.10.93:/root/jerusalem/static/index.html
ssh root@209.222.10.93 'systemctl restart jerusalem'
```
(index.html is served from disk; the restart is for app.py.)

## Verify
1. `systemctl is-active jerusalem` → `active`
2. Dashboard loads (HTTP 200); open a viewer on a stopped/dead instance
   via WebRTC — within ~20s the transport should fall over to H.264/MJPEG
   instead of freezing.
3. Server log shows `[webrtc] <name>: no frames for 15s, closing pc`
   (visible in the dashboard "Eventos" modal).
4. Manual viewer close still works (no failover loop); the 8s startup
   fallback path is unchanged.
