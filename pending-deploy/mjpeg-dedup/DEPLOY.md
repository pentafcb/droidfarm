# mjpeg-dedup — send-on-change MJPEG + keepalive ping

## What it fixes

The MJPEG WebSocket handler (`_handle_ws` in manager/app.py) pushed a
full JPEG at 20Hz **per viewer even when the screen was byte-identical**.
The local screenshot read was already mtime-cached, but the *send* was
not: a static screen (boot, Roblox loading, idle game) cost ~20 identical
JPEGs/sec/viewer of bandwidth for nothing. With adaptive-screencap, an
idle instance captures at 2s intervals, so each frame was re-sent ~40
times before the next capture.

## Changes

manager/app.py (rides apply-all from HEAD):
1. NEW `ws_send_text(sock, text)` — UTF-8 text frame (opcode 0x1), same
   length-encoding ladder as `ws_send_binary`.
2. `_handle_ws` only `ws_send_binary()`s when the frame differs from the
   last one sent (bytes compare; the first frame is always sent, so a
   viewer attaching mid-static-screen gets an image immediately).
3. NEW `WS_PING_SECS = 1.0`: while the source is alive, the server sends
   a tiny text `"ping"` every second. The ping keeps the dashboard's 3s
   "Imagen congelada" stall badge honest (a real stall sends nothing at
   all) and stops idle proxies/NATs from dropping the socket. Pings are
   never sent while the source is dead — a stall must read as a stall.
4. The 15s frameless guard is untouched: it still trips on a dead source
   (`get_frame()` -> None), and its clock still updates on every
   *fetched* frame, changed or not.

manager/static/index.html (rides apply-all from HEAD):
- `startMJPEG`'s `onmessage` still calls `noteFrame()` on every message,
  but returns early for string payloads so the keepalive never touches
  the `<img>`. The stream-freshness block itself is unchanged.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD (app.py +
index.html are already in the stage list). Standalone:

```bash
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
scp manager/static/index.html root@209.222.10.93:/root/jerusalem/static/index.html
ssh root@209.222.10.93 'systemctl restart jerusalem && sleep 2 && systemctl is-active jerusalem'
```

Verify: open a viewer on a static screen (e.g. an instance at the
Android home screen) — the image appears once and stays, the "En vivo"
label stays lit (no "congelada" badge), and the manager log shows no
`[ws] mjpeg` lines. On a stale cached dashboard page the ping would
briefly render a broken image until the next real frame; both files ship
together so this window is negligible.

## Tests

`test_mjpeg_dedup.py`: 30/30 hermetic (real `ws_send_text` frame format:
opcode/length ladder; real `_handle_ws` driven with fakes: static screen
-> 1 binary send + ~1Hz pings, changed frame -> re-sent, no premature
ping, dead source -> guard trips with zero pings, OSError -> clean exit;
source contracts for `WS_PING_SECS`/dedup; dashboard string-guard
contract incl. the stream-freshness `noteFrame()` line).
Preflight t44.
