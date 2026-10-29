# stream-freshness

A "frozen stream" indicator for the dashboard viewer: shows
"Imagen congelada — sin cuadros desde hace Xs" when no video frame arrived
for more than 3 seconds.

## Problem

A stalled stream looked identical to a live one — the last received frame
just sat in the viewer with the "En vivo" label. When Penta reported
streams freezing, there was no way to tell a healthy-but-static screen
(Roblox idling) from a dead pipeline (dead screencap loop, wedged
Waydroid session) without watching the server logs.

## Change

- `manager/static/index.html`:
  - `noteFrame()` is called on every delivered frame on all three
    transports: WebRTC (`ontrack` + `requestVideoFrameCallback` /
    `timeupdate` fallback), H.264 (`ws.onmessage` + rVFC on the video
    element, so a stalled MSE decoder is caught too), MJPEG
    (`ws.onmessage`).
  - A 1s watchdog (`streamAgeTick`) writes into the new `#streamAge`
    element when the frame age exceeds 3s; the badge clears the moment
    frames resume. rVFC chains self-terminate when the viewer closes
    (viewer-open guard), so a closed viewer's chain can never mask a
    real stall in a new session.
  - Lifecycle: `openViewer()` arms the watchdog, `closeViewer()`
    disarms it. During transport fallbacks (WebRTC -> H.264 -> MJPEG)
    the chip honestly shows the gap, then clears on the first frame of
    the new transport.
- Tests: 27/27 pass — the marked freshness block is extracted from the
  page and run behaviorally in node (stubbed DOM/timers/clock): start
  arms one 1s interval, stale ticks show the Spanish badge with the age
  in seconds, a new frame recovers, restart doesn't leak intervals,
  stop clears everything; plus 13 dashboard-contract checks (element,
  CSS, hooks on all 3 transports, lifecycle wiring).

## Deploy

Rides apply-all from HEAD (`manager/static/index.html` already staged).
Suite `t29` in the preflight. Standalone scp (no restart — static file)
in DEPLOY.md. Blocked in worker sessions: no VPS SSH (one test per
session, no retries).
