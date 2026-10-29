# h264-skip-static

## What it is

The shared H.264 broadcaster (`manager/h264_stream.py`) used to run the
libx264 encode at 20fps even when the instance screen was byte-identical —
a static screen (boot, Roblox loading, idle game) cost 20 full encodes/sec
of CPU and ~20 redundant fMP4 fragments/sec of bandwidth per viewer, for
zero visual change. (The MJPEG path got the equivalent send-on-change
optimization in `mjpeg-dedup`; this is the H.264 counterpart.)

Now:

- **Static-skip**: `_run` compares each fetched frame to the last encoded
  frame (bytes compare, post-rescale). Identical frames are not re-encoded
  — except a heartbeat encode every `static_heartbeat` seconds (default
  2.0s), which keeps fragments flowing so the dashboard's 3s "Imagen
  congelada" stall badge never false-fires and late joiners stay within
  one heartbeat of a fresh fragment. A changed frame encodes immediately
  (never waits for the heartbeat).
- **Wall-clock PTS**: the encoder takes `-use_wallclock_as_timestamps 1`
  on the rawvideo input, so skipped frames leave correct PTS gaps instead
  of drifting the "live" view behind real time (frame-count PTS would lag
  ~0.975s per static second — minutes behind after a long idle).
- **`-fps_mode passthrough`**: the default vsync would silently drop
  burst-written frames once timestamps are wall-clock (verified: 25 burst
  frames produced init only). Every fed frame is now encoded with its own
  timestamp.
- **Forced keyframes** (`-force_key_frames expr:gte(t,n_forced*2)`): with
  `frag_keyframe` the muxer only emits a fragment on a keyframe, so
  heartbeat P-frames would buffer unemitted for up to gop/heartbeat
  seconds. A keyframe at least every 2s of media time keeps fragments
  flowing on static screens. During active 20fps streaming the regular
  gop=20 interval already keys every 1s, so this changes nothing there.
- **IDR cache**: `_fragment_has_idr()` scans each completed fragment's
  mdat for an H.264 IDR NAL (type 5); the latest IDR fragment is cached
  and pushed to a new subscriber right after the init segment — instant
  video for late joiners even on a static screen (live fragments alone
  might be P-frames referencing a long-gone IDR).
- **Dead-encoder detection while idle**: previously a dead encoder was
  noticed on the next `write_frame` (every 50ms); with skips that could
  take up to a heartbeat. `_run` now also breaks when `alive()` goes
  false during a skip, so subscribers are closed and the dashboard falls
  back to MJPEG promptly.
- The priming loop in `start()` paces its writes (50ms): wall-clock
  timestamps need spaced writes to stay distinct.

## Blast radius

One file: `manager/h264_stream.py`. No protocol change (still binary fMP4
fragments), no manager changes, no dashboard changes. The broadcaster
constructor gains one optional kwarg (`static_heartbeat=2.0`); the
existing `app.py` call site is untouched.

## Tests

`test_h264_skip_static.py` — 29/29 (stub-encoder skip/heartbeat/change/
dead-encoder/frameless-regression/rescale, `_fragment_has_idr` units,
real-ffmpeg wall-clock PTS via ffprobe: 44/44 packets kept, span matches
wall time, monotonic; real-broadcaster IDR cache + late-subscriber push).

Existing suites updated for the new behavior (counts unchanged):
- `stream-frameless-guard` 24/24: `StubEnc` gained `alive()`; the "frames
  flowing" test now feeds changing frames (a constant frame is now
  legitimately skipped — that is this package's feature, not a stall).
- `h264-mse` 59/59: "first chunk is the init segment" is now
  "first chunk starts with the init segment" (the cached IDR is pushed
  right behind it on subscribe).
