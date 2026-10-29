"""Durable unit tests for the integrated H.264-over-WebSocket streaming.

Covers: h264_stream encoder module, app.py import guard / route /
_handler guards (501/401/404), and static/index.html wiring.

Run:  python3 test_h264_integration.py
"""
import importlib
import io
import os
import re
import sys
import types

PKG = os.path.dirname(os.path.abspath(__file__))
MANAGER = os.path.normpath(os.path.join(PKG, "..", "..", "manager"))
sys.path.insert(0, PKG)
sys.path.insert(0, MANAGER)
os.environ.setdefault("DROIDFARM_DATA", "/tmp/df_h264_test")
os.environ.setdefault("DROIDFARM_PASSWORD", "testonly")

import h264_stream  # noqa: E402
import app  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


# --- 1. encoder module basics -------------------------------------------
check("h264_stream.HAS_FFMPEG true with ffmpeg on PATH", h264_stream.HAS_FFMPEG is True)
check("app.HAS_H264 import guard present", hasattr(app, "HAS_H264"))
check("app.HAS_H264 is True here (ffmpeg+PIL)", app.HAS_H264 is True)

# --- 2. route + method wired --------------------------------------------
check("do_GET routes /ws/h264/", '"/ws/h264/"' in open(os.path.join(MANAGER, "app.py")).read())
check("_handle_ws_h264 method exists", hasattr(app.Handler, "_handle_ws_h264"))
check("h264 501 guard in method", "501" in open(os.path.join(MANAGER, "app.py")).read()[
    open(os.path.join(MANAGER, "app.py")).read().index("def _handle_ws_h264"):][:800])
check("startup banner prints H.264 status",
      'H.264: {HAS_H264}' in open(os.path.join(MANAGER, "app.py")).read())

# --- 3. import-failure fallback: HAS_H264 must go False, not crash --------
saved = sys.modules.get("h264_stream")
sys.modules["h264_stream"] = None  # -> `from x import y` raises ImportError
app2 = importlib.import_module("app")
importlib.reload(app2)
check("HAS_H264 False when h264_stream import fails", app2.HAS_H264 is False)
check("_handle_ws_h264 still defined on import failure",
      hasattr(app2.Handler, "_handle_ws_h264"))
if saved is not None:
    sys.modules["h264_stream"] = saved
else:
    del sys.modules["h264_stream"]
importlib.reload(app2)
app = app2  # restore working module
check("HAS_H264 back to True after restore", app.HAS_H264 is True)


# --- 4. handler guard paths (mocked Handler) ----------------------------
def make_handler(**attrs):
    h = app.Handler.__new__(app.Handler)
    h._send_calls = []
    h._send = lambda code, body=b"", **kw: h._send_calls.append((code, body))
    h.headers = {}
    for k, v in attrs.items():
        setattr(h, k, v)
    return h


h = make_handler()
h._authed_ws = lambda: True
app.HAS_H264 = False  # force encoder-unavailable
h._handle_ws_h264("rbx1")
check("501 when ffmpeg/PIL missing",
      h._send_calls and h._send_calls[0][0] == 501)
app.HAS_H264 = h264_stream.HAS_FFMPEG and app.HAS_PIL

h = make_handler()
h._authed_ws = lambda: False
h._handle_ws_h264("rbx1")
check("401 when unauthenticated",
      h._send_calls and h._send_calls[0][0] == 401)

h = make_handler()
h._authed_ws = lambda: True
orig_instances, orig_providers = app.load_instances, app.load_providers
app.load_instances = lambda: {"rbx9": {"provider": "gone"}}
app.load_providers = lambda: {}
h._handle_ws_h264("rbx9")
check("404 when node unknown",
      h._send_calls and h._send_calls[0][0] == 404
      and h._send_calls[0][1] == b"node not found")
app.load_instances, app.load_providers = orig_instances, orig_providers

# --- 5. encoder round-trip with synthetic frames -------------------------
from PIL import Image  # noqa: E402
W, H = 480, 286
img = Image.new("RGB", (W, H))
px = img.load()
for y in range(H):
    for x in range(W):
        px[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 3) % 256)
frame = img.tobytes()
enc = h264_stream.H264Encoder(width=W, height=H, fps=20, gop=20)
enc.start()
import time  # noqa: E402
init = b""
t_end = time.time() + 8
while time.time() < t_end:
    if not enc.write_frame(frame):
        break
    init += enc.read_available(timeout=0.05)
    if init.startswith(b"\x00\x00\x00\x1cftyp") and b"moov" in init:
        break
check("encoder emits ftyp+moov init segment",
      init.startswith(b"\x00\x00\x00\x1cftyp") and b"moov" in init,
      f"got {len(init)} bytes")
for _ in range(25):
    enc.write_frame(frame)
time.sleep(0.5)
frag = enc.read_available(timeout=0)
check("encoder emits fragments after init", len(frag) > 0, f"{len(frag)} bytes")
check("encoder alive during stream", enc.alive())
enc.close()
check("encoder dead after close", not enc.alive())
check("write after close returns False", enc.write_frame(frame) is False)

# --- 6. odd dims padded to even (H.264 requirement) -----------------------
enc2 = h264_stream.H264Encoder(width=480, height=285)
check("odd height padded to even", enc2.height == 286)

# --- 7. dashboard wiring --------------------------------------------------
html = open(os.path.join(MANAGER, "static", "index.html")).read()
check("webrtc catch falls back to startH264", "startH264(name);" in html)
_cv = html[html.find("function closeViewer(){"):html.find("function closeViewer(){")+400]
check("closeViewer calls stopH264", "stopH264();" in _cv
      and _cv.find("streamFreshnessStop();") < _cv.find("stopH264();"))
check("client hits /ws/h264/ endpoint", "/ws/h264/" in html)
check("H.264 live label set",
      re.search(r"\$\('rbxState'\)\.textContent\s*=\s*'[^']*\(H\.264\)'", html) is not None)
check("startH264 falls back to MJPEG", "function startH264(name){" in html
      and "h264Fail(name); return" in html)
check("MediaSource codec string matches encoder",
      'codecs="avc1.42C015"' in html)
check("old mse_client.js snippet superseded (no stale rbxVideo id)",
      'id="rbxVideo"' not in html)

# --- 8. MP4 box splitter -------------------------------------------------
def _mkbox(btype, payload, largesize=False):
    if largesize:
        return ((1).to_bytes(4, "big") + btype
                + (16 + len(payload)).to_bytes(8, "big") + payload)
    return (8 + len(payload)).to_bytes(4, "big") + btype + payload


_raw = (_mkbox(b"ftyp", b"1234") + _mkbox(b"moov", b"AB")
        + _mkbox(b"moof", b"XY") + _mkbox(b"mdat", b"Z" * 100))
_boxes, _left = h264_stream.split_mp4_boxes(_raw)
check("splitter finds 4 boxes",
      len(_boxes) == 4 and [t for t, _ in _boxes] == [b"ftyp", b"moov", b"moof", b"mdat"])
check("splitter leftover empty on complete input", _left == b"")

_cut = _raw + _mkbox(b"moof", b"Q" * 50)[:30]
_boxes2, _left2 = h264_stream.split_mp4_boxes(_cut)
check("splitter holds incomplete trailing box",
      len(_boxes2) == 4 and len(_left2) == 30)
_boxes3, _left3 = h264_stream.split_mp4_boxes(
    _left2 + _mkbox(b"moof", b"Q" * 50)[30:])
check("splitter resumes after partial feed",
      len(_boxes3) == 1 and _boxes3[0][0] == b"moof" and _left3 == b"")

_big = _mkbox(b"uuid", b"D" * 16, largesize=True)
_boxes4, _ = h264_stream.split_mp4_boxes(_big)
check("splitter handles 64-bit largesize",
      len(_boxes4) == 1 and _boxes4[0][0] == b"uuid")
_bad = b"\x00\x00\x00\x02ABCDrest"
_boxes5, _left5 = h264_stream.split_mp4_boxes(_bad)
check("splitter rejects corrupt size (keeps bytes)",
      _boxes5 == [] and _left5 == _bad)


def _moof_frags(blob):
    """Split a subscriber byte blob into moof-aligned fragments."""
    frags, cur = [], b""
    boxes, _ = h264_stream.split_mp4_boxes(blob)
    for t, d in boxes:
        if t == b"moof":
            if cur:
                frags.append(cur)
            cur = d
        else:
            cur += d
    if cur:
        frags.append(cur)
    return frags


# --- 9. broadcaster end-to-end (real ffmpeg) -------------------------------
import itertools  # noqa: E402

_bframes = []
for _v in range(3):
    _im = Image.new("RGB", (W, H))
    _px = _im.load()
    for _y in range(H):
        for _x in range(W):
            _px[_x, _y] = (((_x * 7) + _v * 40) % 256,
                           ((_y * 13) + _v * 90) % 256,
                           (((_x + _y) * 3) + _v * 130) % 256)
    _bframes.append(_im.tobytes())


def _mk_source():
    it = itertools.cycle(_bframes)
    return lambda: (next(it), W, H)


_bc = h264_stream.H264Broadcaster(_mk_source(), width=W, height=H,
                                  fps=20, gop=20, idle_timeout=30)
check("broadcaster start() primes init", _bc.start(timeout=8) is True)
check("broadcaster init cached (ftyp+moov)",
      _bc._init and _bc._init.startswith(b"\x00\x00\x00\x1cftyp")
      and b"moov" in _bc._init)

_s1 = _bc.subscribe()
_s2 = _bc.subscribe()
check("subscribe returns subscribers", _s1 is not None and _s2 is not None)
check("subscriber_count == 2", _bc.subscriber_count() == 2)
_d1 = _s1.read(timeout=5)
_d2 = _s2.read(timeout=5)
check("subscriber 1 first chunk starts with the init segment",
      _d1[:len(_bc._init)] == _bc._init)
check("subscriber 2 first chunk starts with the init segment",
      _d2[:len(_bc._init)] == _bc._init)

# gather a few seconds of fragments from both subscribers
_blob1, _blob2, _t = b"", b"", time.time()
while time.time() - _t < 3.0:
    _c = _s1.read(timeout=0.5)
    if _c:
        _blob1 += _c
    _c = _s2.read(timeout=0.5)
    if _c:
        _blob2 += _c
_f1, _f2 = _moof_frags(_blob1)[:-1], _moof_frags(_blob2)[:-1]  # drop partial tail
_short, _long = (_f1, _f2) if len(_f1) <= len(_f2) else (_f2, _f1)
check("both subscribers got >=1 complete fragment",
      len(_short) >= 1 and len(_long) >= 1)
check("fan-out: identical fragment sequence (shorter is prefix)",
      _long[:len(_short)] == _short)
check("every fragment moof-aligned",
      all(f[4:8] == b"moof" for f in _short))

# late joiner: init first, then moof-aligned fragments
time.sleep(1.0)
_s3 = _bc.subscribe()
_d3 = _s3.read(timeout=5)
check("late subscriber gets init first",
      _d3[:len(_bc._init)] == _bc._init)
_f3blob, _t = b"", time.time()
while time.time() - _t < 2.5 and not _f3blob:
    _f3blob += _s3.read(timeout=0.5) or b""
check("late subscriber fragments moof-aligned",
      _f3blob[4:8] == b"moof", f"got {_f3blob[4:8]!r}")
_bc.unsubscribe(_s1)
_bc.unsubscribe(_s2)
_bc.unsubscribe(_s3)
check("unsubscribe drops count to 0", _bc.subscriber_count() == 0)
check("closed subscriber read() -> None", _s1.read(timeout=0.5) is None)
_bc.stop()
check("broadcaster stops", not _bc.running)

# --- 10. backpressure: slow viewer skips ahead, stays alive ----------------
_bc2 = h264_stream.H264Broadcaster(_mk_source(), width=W, height=H,
                                   fps=20, gop=20, idle_timeout=30,
                                   max_queue_bytes=2048)
check("small-queue broadcaster starts", _bc2.start(timeout=8) is True)
_ss = _bc2.subscribe()
check("slow sub gets init", _ss.read(timeout=5) == _bc2._init)
time.sleep(2.5)  # never read: queue must stay bounded
# NOTE: fragments are atomic (a slow viewer can be left holding ONE whole
# fragment, which may exceed max_queue_bytes on its own) — boundedness
# means the deque never accumulates many fragments.
check("slow subscriber queue bounded (<=2 fragments held)",
      len(_ss._q) <= 2, f"held={len(_ss._q)} frags, {_ss._queued} bytes")
_tail = _ss.read(timeout=5)
check("slow subscriber still open (not dropped)", _tail is not None)
_tf = _moof_frags(_tail)
check("post-skip fragments still moof-aligned",
      bool(_tf) and all(f[4:8] == b"moof" for f in _tf))
_bc2.unsubscribe(_ss)
_bc2.stop()

# --- 11. idle self-stop reaps the encoder ----------------------------------
_bc3 = h264_stream.H264Broadcaster(_mk_source(), width=W, height=H,
                                   fps=20, gop=10, idle_timeout=0.4)
check("idle-test broadcaster starts", _bc3.start(timeout=8) is True)
_proc = _bc3._enc.proc
_si = _bc3.subscribe()
_si.read(timeout=5)
_bc3.unsubscribe(_si)
_t = time.time()
while _bc3.running and time.time() - _t < 6:
    time.sleep(0.1)
check("broadcaster self-stops with no subscribers", not _bc3.running)
# running flips False when cleanup STARTS; the encoder reap finishes just
# after — wait for it rather than racing it.
_t = time.time()
while _proc.poll() is None and time.time() - _t < 6:
    time.sleep(0.1)
check("encoder process reaped after idle stop", _proc.poll() is not None)

# --- 12. start() fails closed with no frames --------------------------------
_bc4 = h264_stream.H264Broadcaster(lambda: None, width=W, height=H,
                                    idle_timeout=30)
check("start() False when frame_source never yields",
      _bc4.start(timeout=0.5) is False)
check("failed broadcaster not running", not _bc4.running)

# --- 13. manager registry shares one broadcaster per instance ---------------
_jpeg_io = io.BytesIO()
Image.new("RGB", (W, H)).save(_jpeg_io, format="JPEG")
_jpeg_bytes = _jpeg_io.getvalue()
_orig_cap = app.capture_screenshot
app.capture_screenshot = lambda name: _jpeg_bytes
try:
    app._h264_broadcasters.clear()
    _ba = app._get_h264_broadcaster("rbx1", "local", None)
    _bb = app._get_h264_broadcaster("rbx1", "local", None)
    check("registry returns the SAME broadcaster", _ba is not None and _ba is _bb)
    check("registry holds one entry", len(app._h264_broadcasters) == 1)
    _sub = _ba.subscribe()
    _init_chunk = _sub.read(timeout=5) if _sub else b""
    check("registry broadcaster serves init",
          _sub is not None and _init_chunk.startswith(b"\x00\x00\x00\x1cftyp"))
    _ba.unsubscribe(_sub)
    _ba.stop()
    app._h264_broadcasters.clear()
finally:
    app.capture_screenshot = _orig_cap

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
