# h264-skip-static — skip libx264 encodes on byte-identical frames

## What it fixes

The shared H.264 broadcaster (`H264Broadcaster` in manager/h264_stream.py)
re-encoded at 20fps even when the instance screen was byte-identical — a
static screen (boot, Roblox loading, idle game) burned ~20 libx264 encodes
/sec (~4.5ms each measured) and ~20 redundant fMP4 fragments/sec per viewer
for zero visual change. (The MJPEG path got the equivalent send-on-change
optimization in `mjpeg-dedup`; this is the H.264 counterpart.)

## Changes

manager/h264_stream.py only (rides apply-all from HEAD — already in the
stage list). No protocol, manager, or dashboard changes.

1. **Static-skip** in `_run`: each fetched frame (post-rescale bytes) is
   compared to the last encoded frame; identical frames skip
   `write_frame`, except a heartbeat encode every `static_heartbeat`
   seconds (default 2.0s) that keeps fragments flowing so the dashboard's
   3s stall badge never false-fires. Changed frames encode immediately.
2. **Wall-clock PTS**: `-use_wallclock_as_timestamps 1` on the rawvideo
   input, so skipped frames leave correct PTS gaps. (Frame-count PTS
   would drift the "live" view ~1s behind per static second.)
3. **`-fps_mode passthrough`**: the default vsync silently dropped
   burst-written frames once timestamps went wall-clock (verified:
   25-frame burst produced init only). Every fed frame is now encoded.
4. **Forced keyframes** `-force_key_frames expr:gte(t,n_forced*2)`: with
   `frag_keyframe` the muxer only emits a fragment on a keyframe, so
   heartbeat P-frames would buffer unemitted for up to gop/heartbeat
   seconds. A keyframe every 2s of media time keeps fragments flowing on
   static screens; during active 20fps the gop=20 1s interval dominates,
   so nothing changes there.
5. **IDR cache**: `_fragment_has_idr()` scans each completed fragment's
   mdat for an H.264 IDR NAL (type 5, best-effort, never raises); the
   latest IDR fragment is cached and pushed on `subscribe()` right after
   the init segment — late joiners get instant video on static screens.
6. **Dead-encoder detection while idle**: `_run` also breaks when
   `alive()` goes false during a skip (previously detection waited for
   the next write, up to a heartbeat away).
7. `start()` priming loop paces its writes (50ms) — wall-clock
   timestamps need spaced writes to stay distinct.

## Deploy

Rides `pending-deploy/apply-all/deploy-all.sh` from HEAD
(manager/h264_stream.py is already in the stage list). Standalone:

```bash
scp manager/h264_stream.py root@209.222.10.93:/root/jerusalem/h264_stream.py
ssh root@209.222.10.93 'systemctl restart jerusalem && sleep 2 && systemctl is-active jerusalem'
```

Verify: with ffmpeg installed on the VPS, open a viewer on a static
screen — the manager log shows no encode storm, `GET /ws/h264/<name>`
streams, and the "En vivo (H.264)" label stays lit without the
"congelada" badge. (Without ffmpeg the endpoint 501s and the dashboard
falls back to MJPEG, as before — this package changes nothing there.)

## Tests

`test_h264_skip_static.py`: 29/29 (stub-encoder skip/heartbeat/immediate-
change/dead-encoder/frameless-regression/rescale, `_fragment_has_idr`
units incl. garbage-never-raises, real-ffmpeg wall-clock PTS via ffprobe:
44/44 packets kept, span matches wall time, monotonic; real-broadcaster
IDR cache + late-subscriber push).
Preflight t45.

Existing suites updated for the new behavior (counts unchanged):
stream-frameless-guard 24/24, h264-mse 59/59 (see README.md).
