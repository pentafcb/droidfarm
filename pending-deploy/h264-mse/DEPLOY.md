# Deploy: H.264-over-WebSocket streaming (fMP4 + MSE) — STAGED, needs VPS SSH

Built + integrated + unit-tested 2026-10-06 ~21:40 UTC; upgraded to a SHARED
BROADCASTER 2026-10-07 ~00:05 UTC (see below). Supersedes the earlier
`app_h264_snippet.py` / `mse_client.js` staging files (those were merged into
the real sources and deleted; their `rbxVideo`/`rbxScreen` IDs didn't exist).

## What it does
Real H.264 video compression through the Cloudflare tunnel (no UDP/ICE needed,
unlike WebRTC). Viewer chain becomes: WebRTC → H.264/MSE → MJPEG. Measured on
a test clip: ~1.7x smaller than MJPEG with comparable motion fidelity.
Bandwidth on a static Roblox screen is far lower (skip blocks).

v2 (2026-10-07): ONE ffmpeg encoder per instance shared by all viewers
(`H264Broadcaster` in h264_stream.py), instead of one encoder per viewer
connection. Screenshot fetch + JPEG decode also happen once per tick instead
of once per viewer per tick. Fragments are fanned out at moof boundaries so
late joiners get the init segment first, then moof-aligned fragments; slow
viewers skip ahead (oldest fragments dropped) instead of growing memory.
The broadcaster self-stops after 10s with no subscribers. Wire protocol is
UNCHANGED (fMP4 init + fragments over WebSocket) — the dashboard needs no
changes. On an 8-vCPU box running 6 Waydroid instances, viewer #2..N now
costs ~nothing instead of a full extra libx264 encode at 20fps.

Server: `GET /ws/h264/{name}?token=…` — same auth/frame sources as MJPEG.
Client: `startH264()` in index.html, falls back to `startMJPEG()` on any error
(MSE unsupported, 501 from server, append error). `stopH264()` wired into
`closeViewer()`. Dashboard label shows "Live (H.264)".

## Files to deploy (exact commands, run from ~/workspace/droidfarm)
scp manager/h264_stream.py root@209.222.10.93:/root/jerusalem/h264_stream.py
scp manager/app.py root@209.222.10.93:/root/jerusalem/app.py
scp manager/static/index.html root@209.222.10.93:/root/jerusalem/static/index.html
ssh root@209.222.10.93 'which ffmpeg && systemctl restart jerusalem'

## Verify (in order)
1. `systemctl is-active jerusalem` → active
2. `journalctl -u jerusalem -n 5 | grep "Jerusalem 1.0"` → banner shows `H.264: True`
   (False means ffmpeg or Pillow missing on the VPS — check `which ffmpeg`,
   `python3 -c "import PIL"`; endpoint 501s and dashboard silently falls back
   to MJPEG in that case)
3. Unauthenticated probe: `curl -s -o /dev/null -w '%{http_code}' \
   https://<dashboard-host>/ws/h264/rbx1` → 401 (route live, auth enforced)
4. Browser: open a viewer → label should read "Live (H.264)" (WebRTC may fail
   through the tunnel first — expected), stream plays; close viewer → no
   reconnect loop (check dashboard logs for repeated `h264:` lines)
5. If ffmpeg is missing on the VPS: `apt-get install -y ffmpeg` (small,
   from Ubuntu repos) then re-run verify. No other new deps.

## Tests
python3 pending-deploy/h264-mse/test_h264_integration.py → 59/59 pass
(covers import guard, route, 501/401/404 guards, encoder init+fragment
round-trip, close() cleanup, dashboard wiring, MP4 box splitter incl.
largesize/partial/corrupt cases, broadcaster fan-out to 2 subscribers with
identical moof-aligned fragment sequences, late-joiner init-first ordering,
slow-viewer skip-ahead boundedness, idle self-stop + encoder reap,
start() fail-closed, and the manager's per-instance registry sharing).
