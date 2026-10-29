#!/usr/bin/env python3
"""Tests for the WebRTC frameless guard + dashboard post-connect failover.

GAP FIXED: the MJPEG ws handler and the H.264 broadcaster both got 15s
frameless guards (stream-frameless-guard, 2026-10-10), but the WebRTC
ScreenVideoTrack.recv() replayed the last frame FOREVER when the frame
source died (dead screencap loop, wedged Waydroid session, unreachable
wing node). WebRTC is first in the viewer chain, so a wedged pc pinned
the viewer on a frozen stream forever — and the dashboard had no
post-connect failure handling at all (startWebRTC only fell back during
the 8s startup wait).

Now: ScreenVideoTrack takes on_frameless; when no fresh frame arrives
for WS_FRAMELESS_TIMEOUT (15s, same as the other two paths) the track
fires the callback once -> _webrtc_create_offer's _fire_frameless
schedules the idempotent _close_pc("webrtc-frameless") (pops the pc
registry, drops the adaptive-screencap viewer mark, closes the pc).
The dashboard's startWebRTC registers pc.onconnectionstatechange:
failed/closed -> immediate failover to H.264 (which itself falls back
to MJPEG); disconnected -> 5s grace for transient ICE hiccups.

Hermetic: fake aiortc/av (HAS_AIORTC=True), real asyncio loop in a
background thread, real numpy, _get_webrtc_frame stubbed,
WS_FRAMELESS_TIMEOUT shrunk for the timing tests.

Covers:
  A. source wiring: track takes on_frameless, recv checks
     WS_FRAMELESS_TIMEOUT, _webrtc_create_offer passes the callback,
     _fire_frameless schedules _close_pc
  B. fresh frames reset the clock (no fire while frames flow)
  C. stall after frames -> fires once, exactly once
  D. dead from first recv -> fires (clock starts at first recv, not
     construction; the recursive pre-first-frame spin trips it)
  E. raising callback never breaks the track
  F. on_frameless=None -> no crash
  G. full integration: offer -> frameless callback -> pc closed +
     registry popped
  H. dashboard contract: onconnectionstatechange failover to startH264,
     myPc guards, delayed disconnected handling, no innerHTML
"""
import asyncio
import contextlib
import importlib.util
import inspect
import io
import os
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
PAGE = os.path.join(REPO, "manager", "static", "index.html")

PASS = FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok: {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {name} {detail}")


def check(name, cond, detail=""):
    if cond:
        ok(name)
    else:
        bad(name, detail)


# --- fake aiortc / av (installed before app.py import) ------------------------
class _FakeVST:
    def __init__(self, *a, **k):
        pass

    async def next_timestamp(self):
        return (0, 1)


class _FakeSDP:
    def __init__(self, sdp=None, type=None):
        self.sdp = sdp
        self.type = type


class FakePC:
    instances = []

    def __init__(self):
        self.connectionState = "new"
        self._handlers = {}
        self.tracks = []
        self.closed = False
        FakePC.instances.append(self)

    def on(self, event):
        def deco(fn):
            self._handlers[event] = fn
            return fn
        return deco

    def addTrack(self, t):
        self.tracks.append(t)

    async def setRemoteDescription(self, offer):
        self.connectionState = "connecting"

    async def createAnswer(self):
        return _FakeSDP(sdp="v=0 fake-answer", type="answer")

    async def setLocalDescription(self, a):
        self.localDescription = a

    async def close(self):
        self.closed = True
        self.connectionState = "closed"


aiortc = types.ModuleType("aiortc")
aiortc.RTCPeerConnection = FakePC
aiortc.RTCSessionDescription = _FakeSDP
aiortc.VideoStreamTrack = _FakeVST
av = types.ModuleType("av")


class _FakeVF:
    def __init__(self, arr, format):
        self.arr = arr
        self.format = format
        self.pts = None
        self.time_base = None

    @staticmethod
    def from_ndarray(arr, format=None):
        return _FakeVF(arr, format)


av.VideoFrame = _FakeVF
sys.modules["aiortc"] = aiortc
sys.modules["av"] = av

spec = importlib.util.spec_from_file_location("df_app_webrtc_fg", APP)
app = importlib.util.module_from_spec(spec)
sys.modules["df_app_webrtc_fg"] = app  # so inspect.getsource works on classes
spec.loader.exec_module(app)

check("import: HAS_AIORTC true with fake modules", app.HAS_AIORTC is True)
check("import: ScreenVideoTrack present",
      hasattr(app, "ScreenVideoTrack"))
check("import: WS_FRAMELESS_TIMEOUT sane (15s)",
      app.WS_FRAMELESS_TIMEOUT == 15)

LOOP = asyncio.new_event_loop()


def _run_loop():
    asyncio.set_event_loop(LOOP)
    LOOP.run_forever()


threading.Thread(target=_run_loop, daemon=True).start()

# --- frame-source stub ---------------------------------------------------------
SRC = {"mode": "frames"}  # frames | none | slow-frames


def _fake_frame(name, provider):
    mode = SRC["mode"]
    if mode == "none":
        return None
    if mode == "slow-frames":
        time.sleep(0.1)
    return (2, 2, bytes([7]) * 12)


app._get_webrtc_frame = _fake_frame

_real_timeout = app.WS_FRAMELESS_TIMEOUT


def _recv_once(track):
    fut = asyncio.run_coroutine_threadsafe(track.recv(), LOOP)
    return fut.result(timeout=10)


def _drive_until(track, cond, timeout_s=6.0, gap=0.02):
    deadline = time.monotonic() + timeout_s
    while not cond() and time.monotonic() < deadline:
        _recv_once(track)
        time.sleep(gap)
    return cond()


# --- A. source wiring ----------------------------------------------------------
tsrc = inspect.getsource(app.ScreenVideoTrack)
osrc = inspect.getsource(app._webrtc_create_offer)
check("A1 track __init__ takes on_frameless",
      "on_frameless=None" in tsrc)
check("A2 recv checks WS_FRAMELESS_TIMEOUT",
      "WS_FRAMELESS_TIMEOUT" in tsrc)
check("A3 fresh frames reset the clock",
      "self._last_fresh = time.monotonic()" in tsrc)
check("A4 guard fires exactly once (_frameless_fired)",
      "_frameless_fired" in tsrc)
check("A5 callback exceptions swallowed",
      "cb()" in tsrc and "except Exception:" in tsrc)
check("A6 log line names the instance",
      "[webrtc]" in tsrc and "no frames for" in tsrc)
check("A7 _webrtc_create_offer passes on_frameless to the track",
      "on_frameless=_fire_frameless" in osrc)
check("A8 _fire_frameless schedules _close_pc (never blocks)",
      "_fire_frameless" in osrc and "ensure_future(_close_pc(" in osrc
      and '"webrtc-frameless"' in osrc)
check("A9 close stays idempotent for the frameless path",
      '_closed["done"]' in osrc)

# --- B. fresh frames reset the clock -------------------------------------------
app.WS_FRAMELESS_TIMEOUT = 0.4
try:
    SRC["mode"] = "slow-frames"
    fires = []
    t = app.ScreenVideoTrack("w1", "local",
                             on_frameless=lambda: fires.append(1))
    for _ in range(6):  # ~0.6s of flowing frames > 0.4s timeout
        _recv_once(t)
    check("B1 frames flowing for longer than the timeout -> no fire",
          fires == [], f"fires={len(fires)}")
finally:
    SRC["mode"] = "frames"
    app.WS_FRAMELESS_TIMEOUT = _real_timeout

# --- C. stall after frames -> fires once ---------------------------------------
app.WS_FRAMELESS_TIMEOUT = 0.3
try:
    SRC["mode"] = "frames"
    fires = []
    t = app.ScreenVideoTrack("w1", "local",
                             on_frameless=lambda: fires.append(1))
    _recv_once(t)
    _recv_once(t)
    SRC["mode"] = "none"  # source dies; track repeats the last frame
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        tripped = _drive_until(t, lambda: len(fires) == 1)
    check("C1 stall trips the guard", tripped, f"fires={len(fires)}")
    check("C2 one log line names the instance",
          "[webrtc] w1: no frames for 0.3s, closing pc" in buf.getvalue(),
          repr(buf.getvalue()[:120]))
    for _ in range(6):
        _recv_once(t)
    check("C3 no repeat fires on later recvs", len(fires) == 1,
          f"fires={len(fires)}")
finally:
    SRC["mode"] = "frames"
    app.WS_FRAMELESS_TIMEOUT = _real_timeout

# --- D. dead from the very first recv ------------------------------------------
# The pre-first-frame path recurses (sleep 0.1 -> recv) instead of
# returning, so the stub flips to frames once the guard fires and the
# whole chain unwinds with a real frame.
app.WS_FRAMELESS_TIMEOUT = 0.3
t_ref = [None]
orig_fake = app._get_webrtc_frame


def _d_src(name, provider):
    tr = t_ref[0]
    if tr is not None and tr._frameless_fired:
        return (2, 2, bytes([7]) * 12)
    return None


try:
    app._get_webrtc_frame = _d_src
    fires = []
    t = app.ScreenVideoTrack("w1", "local",
                             on_frameless=lambda: fires.append(1))
    t_ref[0] = t
    check("D1 clock not started at construction (slow handshake safe)",
          t._last_fresh is None)
    fr = _recv_once(t)  # returns once the guard fires + source recovers
    check("D2 source dead from first recv -> guard fires",
          len(fires) == 1, f"fires={len(fires)}")
    check("D3 recv returns a real frame after the trip",
          isinstance(fr, _FakeVF) and fr.format == "rgb24")
finally:
    app._get_webrtc_frame = orig_fake
    t_ref[0] = None
    app.WS_FRAMELESS_TIMEOUT = _real_timeout


def _starved_track(cb):
    """Track with a last frame cached and the clock forced 100s stale."""
    t = app.ScreenVideoTrack("w1", "local", on_frameless=cb)
    SRC["mode"] = "frames"
    _recv_once(t)  # caches _last_rgb
    t._last_fresh = time.monotonic() - 100
    SRC["mode"] = "none"
    return t


# --- E. raising callback never breaks the track --------------------------------
def _boom():
    raise RuntimeError("callback boom")


t = _starved_track(_boom)
try:
    fr = _recv_once(t)
    check("E1 raising on_frameless swallowed, last frame still returned",
          isinstance(fr, _FakeVF) and fr.format == "rgb24")
    check("E2 fired flag set despite the raise", t._frameless_fired is True)
finally:
    SRC["mode"] = "frames"

# --- F. on_frameless=None ------------------------------------------------------
t = _starved_track(None)
try:
    fr = _recv_once(t)
    check("F1 None callback: no crash, guard still trips",
          t._frameless_fired is True and isinstance(fr, _FakeVF))
finally:
    SRC["mode"] = "frames"

# --- G. full integration: offer -> frameless callback -> pc closed -------------
BASE = tempfile.mkdtemp(prefix="df-webrtc-fg-")
app._STREAM_WANT_BASE = BASE
app._stream_viewers.clear()
app._webrtc_pcs.clear()
FakePC.instances.clear()
try:
    SRC["mode"] = "frames"
    fut = asyncio.run_coroutine_threadsafe(
        app._webrtc_create_offer("w1", "local", "v=0 fake-offer",
                                 lambda *a: None),
        LOOP)
    fut.result(timeout=5)
    pc = FakePC.instances[-1]
    check("G1 offer created a pc with a video track",
          len(pc.tracks) == 1)
    track = pc.tracks[0]
    check("G2 track carries the frameless callback",
          callable(getattr(track, "_on_frameless", None)))

    async def _invoke_cb():
        track._on_frameless()

    asyncio.run_coroutine_threadsafe(_invoke_cb(), LOOP).result(timeout=5)
    deadline = time.monotonic() + 5
    while not pc.closed and time.monotonic() < deadline:
        time.sleep(0.05)
    check("G3 frameless callback closed the pc", pc.closed is True)
    check("G4 pc popped from the registry",
          not any((p[0] is pc) if isinstance(p, tuple) else (p is pc)
                  for p in app._webrtc_pcs.values()))
finally:
    SRC["mode"] = "frames"
    app._webrtc_pcs.clear()
    FakePC.instances.clear()

# --- H. dashboard contract ------------------------------------------------------
with open(PAGE, encoding="utf-8") as f:
    page = f.read()
i = page.index("async function startWebRTC(name){")
region = page[i:i + 4600]
check("H1 onconnectionstatechange handler registered",
      "pc.onconnectionstatechange" in region)
check("H2 failed/closed -> failover to startH264",
      "st === 'failed'" in region
      and "st === 'closed'" in region
      and "startH264(name)" in region)
check("H3 myPc guard prevents failover on manual close",
      "myPc !== pc" in region)
check("H4 disconnected gets a delayed re-check (not instant failover)",
      "'disconnected'" in region and "5000" in region)
check("H5 failover nulls pc/dc before starting H.264",
      "pc = null; dc = null;" in region)
check("H6 no innerHTML in the failover block",
      "innerHTML" not in region)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
