# DEPLOY: ws-reader-timeout

## What

Timeout-as-close fix in `manager/app.py`: `ws_read_frame()` now returns
the `WS_READ_TIMEOUT` (-1) sentinel on a recv timeout instead of
`(None, None)`, and both stream readers (MJPEG `_handle_ws`, H.264
`_handle_ws_h264`) `continue` on it instead of stopping. Without it,
every MJPEG/H.264 viewer was killed ~10s after opening — both sockets
run with `settimeout(10)`, the dashboard client never sends frames on
them (input goes over the WebRTC data channel), and `socket.timeout`
is an `OSError` subclass, so the idle timeout fell into the
close/error handler and looked exactly like a client disconnect. The
viewer then reconnected in a loop (MJPEG 3s retry; H.264 fail ->
MJPEG). See README.md.

## How

Rides `pending-deploy/apply-all/deploy-all.sh` from repo HEAD — no
standalone deploy needed. The stage list already contains
`manager/app.py`; no new stage files, no dashboard change, no wing
agent change, no VERSION bump.

## Standalone (if apply-all is unavailable)

```sh
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
ssh root@209.222.10.93 'systemctl restart jerusalem'
```

## Verify

1. `systemctl is-active jerusalem` -> active.
2. Dashboard root returns HTTP 200.
3. Sign in, open a viewer on a running instance, force the MJPEG
   fallback (or pick an instance whose WebRTC path fails): the stream
   stays up well past 10s with no reconnect flicker, and the
   "Imagen congelada" stall badge stays hidden while frames flow.
4. Close the viewer: the stream stops (reader still stops on a real
   close frame / peer close, and the frameless guard still closes a
   dead source after 15s) — no leaked fast-capture marker.
5. Open a viewer on a stopped instance: the socket is still rejected
   (409) and the MJPEG terminal guard still stops after 3
   never-opened closes — the timeout fix changes nothing about
   rejection handling.
