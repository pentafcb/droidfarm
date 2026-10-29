#!/usr/bin/env python3
"""Tests for the WebRTC adaptive-screencap hook.

GAP FIXED: adaptive-screencap (2026-10-11) hooked the MJPEG ws handler and
the H.264 ws handler into the stream_wanted viewer registry, but NOT the
WebRTC peer-connection path — its ScreenVideoTrack reads live.png directly.
A WebRTC-only viewer would therefore get 2s-interval frames once
adaptive-screencap deploys. Now _webrtc_create_offer calls
stream_viewer_open after the answer is set (failed handshakes never leak a
mark) and an idempotent _close_pc drops it on failed/closed/disconnected,
plus a stale-handshake sweep (default 180s) for pcs that never leave
new/connecting. Remote providers stay a no-op (the agent owns its own
marker via screenshot-fetch traffic).

Hermetic: fake aiortc/av modules (HAS_AIORTC=True), real asyncio loop in a
background thread, _STREAM_WANT_BASE redirected to a tmpdir.

Covers:
  A. wiring contract (source): open/close calls present, constant sane,
     ws hooks intact
  B. happy path: answer -> registry 1 -> marker -> close on disconnected
  C. idempotent close: disconnected then closed -> single decrement
  D. remote provider: no-op
  E. handshake failure: setRemoteDescription raises -> no mark leaked
  F. stale sweep: stuck connecting -> swept; connected -> untouched;
     sweep after normal close -> no double decrement
  G. never-raises: raising pc.close still drops the mark
  H. retouch integration: stream_want_retouch keeps webrtc marks fresh
  I. multi-viewer refcounting
"""
import asyncio
import importlib.util
import inspect
import os
import shutil
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")

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
        self.raise_on_close = False
        self.fail_remote = False
        self.localDescription = None
        FakePC.instances.append(self)

    def on(self, event):
        def deco(fn):
            self._handlers[event] = fn
            return fn
        return deco

    def addTrack(self, t):
        self.tracks.append(t)

    async def setRemoteDescription(self, offer):
        if self.fail_remote:
            raise RuntimeError("bad sdp")
        self.connectionState = "connecting"

    async def createAnswer(self):
        return _FakeSDP(sdp="v=0 fake-answer", type="answer")

    async def setLocalDescription(self, a):
        self.localDescription = a

    async def close(self):
        if self.raise_on_close:
            raise RuntimeError("close boom")
        self.closed = True
        self.connectionState = "closed"


aiortc = types.ModuleType("aiortc")
aiortc.RTCPeerConnection = FakePC
aiortc.RTCSessionDescription = _FakeSDP
aiortc.VideoStreamTrack = _FakeVST
av = types.ModuleType("av")


class _FakeVF:
    @staticmethod
    def from_ndarray(arr, format=None):
        return ("frame", arr, format)


av.VideoFrame = _FakeVF
sys.modules["aiortc"] = aiortc
sys.modules["av"] = av

spec = importlib.util.spec_from_file_location("df_app_webrtc", APP)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

BASE = tempfile.mkdtemp(prefix="df-webrtc-want-")
app._STREAM_WANT_BASE = BASE  # redirect the module constant for hermetic tests

check("import: HAS_AIORTC true with fake modules", app.HAS_AIORTC is True)
check("import: _webrtc_create_offer present",
      callable(getattr(app, "_webrtc_create_offer", None)))

LOOP = asyncio.new_event_loop()


def _run_loop():
    asyncio.set_event_loop(LOOP)
    LOOP.run_forever()


threading.Thread(target=_run_loop, daemon=True).start()


def offer(name="w1", provider="local", fail_remote=False):
    FakePC.instances.clear()
    if fail_remote:
        orig = FakePC.setRemoteDescription

        async def boom(self, offer):
            raise RuntimeError("bad sdp")
        FakePC.setRemoteDescription = boom
    try:
        fut = asyncio.run_coroutine_threadsafe(
            app._webrtc_create_offer(name, provider, "v=0 fake-offer",
                                     lambda *a: None),
            LOOP)
        return fut.result(timeout=5), (FakePC.instances[-1]
                                       if FakePC.instances else None)
    finally:
        if fail_remote:
            FakePC.setRemoteDescription = orig


def fire_state(pc, state):
    pc.connectionState = state
    h = pc._handlers["connectionstatechange"]
    return asyncio.run_coroutine_threadsafe(h(), LOOP).result(timeout=5)


def fresh():
    app._stream_viewers.clear()
    app._webrtc_pcs.clear()
    FakePC.instances.clear()
    for root, dirs, files in os.walk(BASE):
        for f in files:
            os.remove(os.path.join(root, f))


def marker(name):
    p = app._stream_want_path(name)
    return p if (p and os.path.exists(p)) else None


# --- A. wiring contract -------------------------------------------------------
src = inspect.getsource(app._webrtc_create_offer)
check("A1 _webrtc_create_offer calls stream_viewer_open",
      "stream_viewer_open(provider, instance_name)" in src)
check("A2 _webrtc_create_offer calls stream_viewer_close",
      "stream_viewer_close(provider, instance_name)" in src)
check("A3 stale sweep scheduled via call_later",
      "call_later(_WEBRTC_STALE_SECS" in src and "_stale_sweep" in src)
check("A4 _WEBRTC_STALE_SECS sane", 60 <= app._WEBRTC_STALE_SECS <= 600,
      f"got {app._WEBRTC_STALE_SECS}")
check("A5 open happens only after setLocalDescription",
      src.index("setLocalDescription(answer)") <
      src.index("stream_viewer_open(provider, instance_name)"))
check("A6 close is idempotent (_closed guard)",
      '_closed["done"]' in src)
ws_src = inspect.getsource(app.Handler._handle_ws)
h264_src = inspect.getsource(app.Handler._handle_ws_h264)
check("A7 _handle_ws hook intact",
      "stream_viewer_open(prov, name)" in ws_src
      and "stream_viewer_close(prov, name)" in ws_src)
check("A8 _handle_ws_h264 hook intact",
      "stream_viewer_open(prov, name)" in h264_src
      and "stream_viewer_close(prov, name)" in h264_src)

# --- B. happy path ------------------------------------------------------------
fresh()
sdp, pc = offer("w1", "local")
check("B1 answer SDP returned", isinstance(sdp, str) and "fake-answer" in sdp)
check("B2 registry has one local viewer",
      app._stream_viewers.get(("local", "w1")) == 1,
      str(app._stream_viewers))
check("B3 marker file created", marker("w1") is not None)
check("B4 pc registered in _webrtc_pcs with (pc, provider, instance)",
      len(app._webrtc_pcs) == 1
      and any(isinstance(v, tuple) and len(v) == 3 and v[0] is pc
              and v[1] == "local" and v[2] == "w1"
              for v in app._webrtc_pcs.values()))
fire_state(pc, "disconnected")
check("B5 disconnected closes the pc", pc.closed is True)
check("B6 disconnected pops _webrtc_pcs", len(app._webrtc_pcs) == 0)
check("B7 disconnected drops the viewer mark",
      app._stream_viewers.get(("local", "w1")) is None
      and marker("w1") is None)

# --- C. idempotent close ------------------------------------------------------
fresh()
sdp, pc = offer("w2", "local")
fire_state(pc, "disconnected")
fire_state(pc, "closed")  # aiortc often fires both
check("C1 double close: no registry entry, no negative",
      ("local", "w2") not in app._stream_viewers)
check("C2 double close: no exception, pc count sane",
      len(app._webrtc_pcs) == 0)
fresh()
sdp, pc = offer("w2", "local")
fire_state(pc, "failed")
check("C3 failed state also closes", pc.closed and len(app._webrtc_pcs) == 0
      and ("local", "w2") not in app._stream_viewers)

# --- D. remote provider no-op -------------------------------------------------
fresh()
sdp, pc = offer("s1", "srt")
check("D1 remote offer still answers", "fake-answer" in sdp)
check("D2 remote: registry untouched", app._stream_viewers == {})
check("D3 remote: no marker created", marker("s1") is None)
fire_state(pc, "disconnected")
check("D4 remote close: still no registry entry", app._stream_viewers == {})

# --- E. handshake failure leaks nothing --------------------------------------
fresh()
try:
    offer("w3", "local", fail_remote=True)
    raised = False
except RuntimeError:
    raised = True
check("E1 bad SDP raises", raised)
check("E2 failed handshake: registry empty", app._stream_viewers == {})
check("E3 failed handshake: no marker", marker("w3") is None)

# --- F. stale sweep -----------------------------------------------------------
fresh()
old = app._WEBRTC_STALE_SECS
app._WEBRTC_STALE_SECS = 0.3
try:
    sdp, pc = offer("w4", "local")  # stuck in "connecting", no state events
    check("F1 stuck pc is connecting", pc.connectionState == "connecting")
    time.sleep(0.9)  # let the 0.3s sweep fire on the loop
    check("F2 stale sweep closed the pc", pc.closed is True)
    check("F3 stale sweep popped _webrtc_pcs", len(app._webrtc_pcs) == 0)
    check("F4 stale sweep dropped the viewer mark",
          ("local", "w4") not in app._stream_viewers and marker("w4") is None)

    fresh()
    sdp, pc = offer("w5", "local")
    pc.connectionState = "connected"  # ICE made it before the sweep
    time.sleep(0.9)
    check("F5 connected pc survives the sweep", pc.closed is False)
    check("F6 connected viewer mark survives",
          app._stream_viewers.get(("local", "w5")) == 1
          and marker("w5") is not None)
    fire_state(pc, "closed")
    time.sleep(0.9)  # sweep fires again after a normal close
    check("F7 sweep after close: no double decrement",
          ("local", "w5") not in app._stream_viewers)
finally:
    app._WEBRTC_STALE_SECS = old

# --- G. never-raises ----------------------------------------------------------
fresh()
sdp, pc = offer("w6", "local")
pc.raise_on_close = True
try:
    fire_state(pc, "disconnected")
    raised = False
except Exception as e:  # noqa: BLE001
    raised = True
check("G1 raising pc.close does not propagate", not raised)
check("G2 mark still dropped despite close failure",
      ("local", "w6") not in app._stream_viewers and marker("w6") is None)

# --- H. retouch integration ---------------------------------------------------
fresh()
sdp, pc = offer("w7", "local")
p = marker("w7")
old_mtime = os.stat(p).st_mtime
os.utime(p, (old_mtime - 100, old_mtime - 100))
app.stream_want_retouch()
check("H1 stream_want_retouch refreshes the webrtc marker",
      os.stat(p).st_mtime > old_mtime - 50)
fire_state(pc, "closed")

# --- I. multi-viewer refcounting ----------------------------------------------
fresh()
sdp1, pc1 = offer("w8", "local")
sdp2, pc2 = offer("w8", "local")
check("I1 two viewers -> count 2",
      app._stream_viewers.get(("local", "w8")) == 2)
fire_state(pc1, "closed")
check("I2 one close -> count 1, marker kept",
      app._stream_viewers.get(("local", "w8")) == 1
      and marker("w8") is not None)
fire_state(pc2, "closed")
check("I3 last close -> count 0, marker cleared",
      ("local", "w8") not in app._stream_viewers and marker("w8") is None)

LOOP.call_soon_threadsafe(LOOP.stop)
shutil.rmtree(BASE, ignore_errors=True)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
