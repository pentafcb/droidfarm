"""Tests for H.264 static-skip: skip libx264 encodes on byte-identical frames.

The shared broadcaster used to re-encode at 20fps even when the screen was
byte-identical (static screens: boot, loading, idle game) — pure wasted
encoder CPU and ~20 redundant fMP4 fragments/sec. Now: identical frames are
not re-encoded, except a heartbeat encode every static_heartbeat seconds
(keeps the dashboard's 3s stall badge honest); the encoder uses wall-clock
input timestamps so skipped frames leave correct PTS gaps instead of
drifting the "live" view behind real time; the latest IDR fragment is cached
and pushed to late subscribers for instant video on static screens.

Run: python3 test_h264_skip_static.py
"""
import itertools
import os
import subprocess
import sys
import threading
import time

PKG = os.path.dirname(os.path.abspath(__file__))
MANAGER = os.path.normpath(os.path.join(PKG, "..", "..", "manager"))
sys.path.insert(0, MANAGER)

import h264_stream  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


W, H = 480, 284


def frame(v):
    return bytes([v % 256]) * W * H * 3


class StubEnc:
    def __init__(self):
        self.writes = 0
        self.closed = False
        self._alive = True

    def write_frame(self, rgb):
        self.writes += 1
        return True

    def read_available(self, timeout=0.0):
        return b""

    def alive(self):
        return self._alive

    def close(self):
        self.closed = True


def make_bc(source, heartbeat=0.3, frameless_timeout=60):
    bc = h264_stream.H264Broadcaster(source, width=W, height=H, fps=20,
                                     gop=20, idle_timeout=60,
                                     frameless_timeout=frameless_timeout,
                                     static_heartbeat=heartbeat)
    stub = StubEnc()
    bc._enc = stub
    bc._enc_w, bc._enc_h = W, H
    bc._init = b"fake-init"
    bc._stub = stub
    return bc


def run_bc(bc, secs):
    t = threading.Thread(target=bc._run, daemon=True)
    t.start()
    time.sleep(secs)
    bc.stop()
    t.join(timeout=5)
    return t


# --- 1. static frames are skipped (stub encoder) ---------------------------
bc = make_bc(lambda: (frame(10), W, H), heartbeat=0.3)
t = run_bc(bc, 1.0)
check("static screen: thread ran and stopped cleanly", not t.is_alive())
# 20fps for 1.0s would be 20 writes; static-skip allows first + heartbeats
check("static screen: encodes skipped (writes << 20)",
      1 <= bc._stub.writes <= 7, f"writes={bc._stub.writes}")
check("static screen: heartbeat encodes still flow",
      bc._stub.writes >= 2, f"writes={bc._stub.writes}")

# --- 2. changing frames are never skipped -----------------------------------
ctr = itertools.count()


def changing():
    return (frame(next(ctr)), W, H)


bc2 = make_bc(changing, heartbeat=999)  # heartbeat disabled
t2 = run_bc(bc2, 0.5)
check("changing frames: every tick encoded (no skip)",
      bc2._stub.writes >= 8, f"writes={bc2._stub.writes}")

# --- 3. a single change encodes immediately (no heartbeat wait) -------------
state = {"v": 10}
bc3 = make_bc(lambda: (frame(state["v"]), W, H), heartbeat=999)
t3 = threading.Thread(target=bc3._run, daemon=True)
t3.start()
time.sleep(0.3)
w_before = bc3._stub.writes
state["v"] = 200  # screen changes once, then static again
time.sleep(0.3)
w_after = bc3._stub.writes
bc3.stop()
t3.join(timeout=5)
check("single change: encoded immediately",
      w_after == w_before + 1, f"before={w_before} after={w_after}")

# --- 4. encoder death during a static skip is detected ----------------------
bc4 = make_bc(lambda: (frame(7), W, H), heartbeat=999)
bc4._stub._alive = False
t4 = threading.Thread(target=bc4._run, daemon=True)
t4.start()
t4.join(timeout=5)
check("dead encoder during static skip: _run exits", not t4.is_alive())
check("dead encoder during static skip: marked stopped", bc4._stopped)
check("dead encoder during static skip: encoder reaped",
      bc4._stub.closed and bc4._enc is None)

# --- 5. frameless guard still trips when the source dies --------------------
bc5 = make_bc(lambda: None, frameless_timeout=0.4)
t5 = threading.Thread(target=bc5._run, daemon=True)
t5.start()
t5.join(timeout=5)
check("frameless guard regression: trips on dead source", not t5.is_alive())

# --- 6. odd-size frames: compare happens after rescale, no crash -------------
bc6 = make_bc(lambda: (frame(33), 240, 142), heartbeat=0.3)
t6 = run_bc(bc6, 0.6)
check("rescaled static frames skipped without error",
      not t6.is_alive() and 1 <= bc6._stub.writes <= 5,
      f"writes={bc6._stub.writes}")

# --- 7. constructor contract -------------------------------------------------
bc7 = h264_stream.H264Broadcaster(lambda: None)
check("default static_heartbeat is 2.0s", bc7._static_heartbeat == 2.0)
check("skip state initialized",
      bc7._last_encoded is None and bc7._last_idr is None)

# --- 8. _fragment_has_idr ----------------------------------------------------


def fake_mdat(nal_types):
    """Build a minimal mdat box with length-prefixed NAL units."""
    body = b""
    for nt in nal_types:
        payload = bytes([0x10, 0x20, 0x30])
        hdr = bytes([(nt & 0x1F) | 0x60])
        nal = hdr + payload
        body += len(nal).to_bytes(4, "big") + nal
    return (8 + len(body)).to_bytes(4, "big") + b"mdat" + body


def fake_box(btype, body=b""):
    return (8 + len(body)).to_bytes(4, "big") + btype + body


check("idr: fragment with IDR NAL -> True",
      h264_stream._fragment_has_idr(fake_box(b"moof") + fake_mdat([1, 5, 1])))
check("idr: fragment with only P NALs -> False",
      not h264_stream._fragment_has_idr(fake_box(b"moof") + fake_mdat([1, 1])))
check("idr: init-only bytes -> False",
      not h264_stream._fragment_has_idr(fake_box(b"ftyp") + fake_box(b"moov")))
check("idr: garbage never raises -> False",
      not h264_stream._fragment_has_idr(b"junk-bytes"))
check("idr: empty never raises -> False",
      not h264_stream._fragment_has_idr(b""))
check("idr: truncated NAL length never raises -> False",
      not h264_stream._fragment_has_idr(fake_box(b"mdat", b"\xff\xff")))

# --- 9. wall-clock PTS with skipped frames (real ffmpeg + ffprobe) ------------
# Simulates the broadcaster's static-skip: paced frames, a gap with nothing
# fed, paced frames again. Wall-clock input timestamps must keep the PTS
# honest (span ~= wall time); frame-count timestamps would drift (span ~=
# frames/fps, growing the "live" latency on every static period).
enc = h264_stream.H264Encoder(width=W, height=H, fps=20, gop=20)
enc.start()
for _ in range(22):
    enc.write_frame(frame(10))
    time.sleep(0.05)  # paced like the broadcaster's fps tick
GAP = 1.0
time.sleep(GAP)  # the static-skip gap: nothing encoded
for _ in range(22):
    enc.write_frame(frame(200))
    time.sleep(0.05)
enc.proc.stdin.close()  # EOF: ffmpeg flushes the trailing fragment, exits
out = b""
t_end = time.time() + 10
while time.time() < t_end:
    chunk = enc.read_available(timeout=0.5)
    out += chunk
    if enc.proc.poll() is not None and not chunk:
        out += enc.read_available(timeout=0)
        break
enc.close()
check("wallclock: encoder produced fMP4 output", len(out) > 1000,
      f"bytes={len(out)}")
with open("/tmp/h264_skip_pts.mp4", "wb") as f:
    f.write(out)
probe = subprocess.run(
    ["ffprobe", "-hide_banner", "-loglevel", "error",
     "-show_packets", "-select_streams", "v", "/tmp/h264_skip_pts.mp4"],
    capture_output=True, text=True, timeout=15)
pts = [float(l.split("=")[1]) for l in probe.stdout.splitlines()
       if l.startswith("pts_time=")]
check("wallclock: no frames dropped by timestamp collisions",
      len(pts) == 44, f"n={len(pts)}")
span = (max(pts) - min(pts)) if pts else -1
# frame-count PTS would give 43/20 = 2.15s; wall-clock gives ~3.2s
check("wallclock: PTS span matches wall time (no drift)",
      2.8 <= span <= 4.5, f"span={span:.2f}")
check("wallclock: PTS monotonic",
      all(b >= a for a, b in zip(pts, pts[1:])))

# --- 10. IDR cache + late subscriber (real ffmpeg) -----------------------------
from PIL import Image  # noqa: E402

_bframes = []
for _v in range(3):
    _im = Image.new("RGB", (W, H))
    _px = _im.load()
    for _y in range(0, H, 4):
        for _x in range(0, W, 4):
            _px[_x, _y] = (((_x * 7) + _v * 40) % 256,
                           ((_y * 13) + _v * 90) % 256,
                           (((_x + _y) * 3) + _v * 130) % 256)
    _bframes.append(_im.tobytes())


def _mk_source():
    it = itertools.cycle(_bframes)
    return lambda: (next(it), W, H)


bc8 = h264_stream.H264Broadcaster(_mk_source(), width=W, height=H,
                                  fps=20, gop=20, idle_timeout=30,
                                  static_heartbeat=999)
check("real broadcaster primes with wallclock encoder", bc8.start(timeout=8))
check("start() primes the skip state", bc8._last_encoded is not None)
_sub = bc8.subscribe()
_blob, _t = b"", time.time()
while time.time() - _t < 4.0 and bc8._last_idr is None:
    _c = _sub.read(timeout=0.5)
    if _c:
        _blob += _c
check("IDR fragment cached while streaming", bc8._last_idr is not None)
check("cached fragment really holds an IDR",
      bc8._last_idr is not None
      and h264_stream._fragment_has_idr(bc8._last_idr))
bc8.unsubscribe(_sub)
# late subscriber: init first, then the cached IDR right behind it
_sL = bc8.subscribe()
_dL = _sL.read(timeout=5)
check("late subscriber: init segment first",
      _dL[:len(bc8._init)] == bc8._init)
check("late subscriber: cached IDR pushed after init",
      bc8._last_idr in _dL)
bc8.unsubscribe(_sL)
bc8.stop()
check("broadcaster stops cleanly", not bc8.running)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
