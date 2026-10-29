# DEPLOY: webrtc-stream-want (WebRTC viewers hook into adaptive-screencap)

## The gap

`adaptive-screencap` (2026-10-11) hooked the MJPEG ws handler and the H.264
ws handler into the `stream_wanted` viewer registry, but NOT the WebRTC
peer-connection path — its `ScreenVideoTrack` reads `live.png` directly.
Once adaptive-screencap deploys, a WebRTC-only viewer would silently get
2s-interval frames (idle mode) while the browser shows a live-looking view.

## What it deploys

- `manager/app.py` -> `/root/jerusalem/app.py`
  (`_webrtc_create_offer`: `stream_viewer_open(provider, instance_name)`
  after the answer is set — a failed handshake never leaks a mark; the
  state-change teardown is now an idempotent `_close_pc()` that also calls
  `stream_viewer_close`; a 180s stale-handshake sweep (`call_later` on the
  webrtc loop) closes pcs stuck in new/connecting so an abandoned handshake
  can't pin the fast capture loop forever. Remote providers stay a no-op —
  the agent owns its own marker via screenshot-fetch traffic. The 15s
  `stream_want_retouch()` from the resource poller keeps long-lived WebRTC
  viewers' markers fresh with zero extra wiring.)

Already rides the apply-all stage list from HEAD — no new stage files.
Preferred path: run `pending-deploy/apply-all/deploy-all.sh` from an
SSH-capable session (preflight runs this package's suite as t42).

## Standalone deploy (SSH-capable session only)

```bash
# from ~/workspace/droidfarm:
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'python3 -m py_compile /root/jerusalem/app.py && systemctl restart jerusalem'
```

## Verify

```bash
ssh root@209.222.10.93 'grep -c "_close_pc" /root/jerusalem/app.py'
# expect >= 4 (def + on_state call + sweep call + docstring)
ssh root@209.222.10.93 'systemctl is-active jerusalem && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/'
# expect: active / 200
```

Functional check (browser): open any instance viewer, let it negotiate
WebRTC (status "Live (WebRTC)"), then on the VPS:
`stat -c %y /var/lib/docker/volumes/wd-<name>-data/_data/data/media/0/jerusalem/stream_wanted`
should stay fresh (<30s old) while the WebRTC view is open, and the capture
loop (`docker exec wd-<name> ps aux | grep screencap`) runs at 0.12s.
Close the viewer: the marker disappears within a minute.
