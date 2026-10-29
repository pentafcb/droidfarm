#!/usr/bin/env python3
"""Tests for the WebRTC failed-handshake immediate cleanup.

GAP FIXED: in manager/app.py::_webrtc_create_offer, a failed handshake
(garbage SDP -> RTCSessionDescription/setRemoteDescription raises, or a
failed createAnswer/setLocalDescription) left the RTCPeerConnection and
its _webrtc_pcs registry entry alive until the 180s stale-handshake sweep
reaped them — up to 3 minutes of leaked threads/UDP sockets per failed
offer. The offer route is auth-gated, but a buggy client retry loop (or a
wedged loop racing fut.result's 15s timeout) could accumulate them.

Now the handshake (SDP parse -> setRemoteDescription -> createAnswer ->
setLocalDescription) is wrapped in try/except: on failure the already
idempotent _close_pc("handshake-failed") runs immediately (registry pop,
pc.close()), then the exception re-raises so webrtc_offer_sync still
returns None and the dashboard falls back to H.264/MJPEG. The viewer mark
is untouched by the failure path — it opens only after setLocalDescription
succeeds, and stream_viewer_close clamps at 0 anyway.

Hermetic: fake aiortc/av (HAS_AIORTC=True), real asyncio loop in a
background thread, real numpy, _STREAM_WANT_BASE redirected to a tmpdir.

Covers:
  A. wiring contract: handshake wrapped in try/except, _close_pc with the
     handshake-failed reason, re-raise preserved
  B. fail at RTCSessionDescription parse -> pc closed, registry popped,
     exception propagates
  C. fail at setRemoteDescription -> pc closed, registry popped
  D. fail at createAnswer -> pc closed, registry popped
  E. fail at setLocalDescription -> pc closed, registry popped
  F. failed handshake: no viewer mark opened, stream_viewers untouched
     (close clamps harmlessly at 0)
  G. happy path unchanged: pc open, registry holds the entry, viewer mark
     opened + marker file present
  H. the scheduled stale sweep after a cleaned-up failure is a no-op
     (pc stays closed, registry stays empty)
  I. _close_pc idempotency on the failure path (double close harmless)
"""
import asyncio
import importlib.util
import inspect
import os
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

    async def next_timestamp(self):
        return (0, 1)


class _FakeSDP:
    fail_parse = False

    def __init__(self, sdp=None, type=None):
        if _FakeSDP.fail_parse:
            raise ValueError("bad sdp")
        self.sdp = sdp
        self.type = type


class FakePC:
    instances = []
    fail_at = None  # None | "setRemoteDescription" | "createAnswer" | "setLocalDescription"

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
        if FakePC.fail_at == "setRemoteDescription":
            raise RuntimeError("bad sdp")
        self.connectionState = "connecting"

    async def createAnswer(self):
        if FakePC.fail_at == "createAnswer":
            raise RuntimeError("answer failed")
        return _FakeSDP(sdp="v=0 fake-answer", type="answer")

    async def setLocalDescription(self, a):
        if FakePC.fail_at == "setLocalDescription":
            raise RuntimeError("local desc failed")
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

spec = importlib.util.spec_from_file_location("df_app_webrtc_hc", APP)
app = importlib.util.module_from_spec(spec)
sys.modules["df_app_webrtc_hc"] = app
spec.loader.exec_module(app)

BASE = tempfile.mkdtemp(prefix="df-webrtc-hc-")
app._STREAM_WANT_BASE = BASE  # redirect the module constant for hermetic tests

check("import: HAS_AIORTC true with fake modules", app.HAS_AIORTC is True)

LOOP = asyncio.new_event_loop()


def _run_loop():
    asyncio.set_event_loop(LOOP)
    LOOP.run_forever()


threading.Thread(target=_run_loop, daemon=True).start()


def fresh():
    app._stream_viewers.clear()
    app._webrtc_pcs.clear()
    FakePC.instances.clear()
    FakePC.fail_at = None
    _FakeSDP.fail_parse = False
    for root, dirs, files in os.walk(BASE):
        for f in files:
            os.remove(os.path.join(root, f))


def offer(name="w1", provider="local"):
    fut = asyncio.run_coroutine_threadsafe(
        app._webrtc_create_offer(name, provider, "v=0 fake-offer",
                                 lambda *a: None),
        LOOP)
    return fut.result(timeout=10), (FakePC.instances[-1]
                                    if FakePC.instances else None)


def marker(name):
    p = app._stream_want_path(name)
    return p if (p and os.path.exists(p)) else None


# --- A. wiring contract -------------------------------------------------------
src = inspect.getsource(app._webrtc_create_offer)
check("A1 handshake wrapped in try/except",
      "await _close_pc(\"handshake-failed\")" in src)
check("A2 failure re-raises (offer still fails fast to fallback)",
      "await _close_pc(\"handshake-failed\")\n        raise" in src)
check("A3 SDP parse inside the guarded block",
      src.index("try:") < src.index("RTCSessionDescription(sdp=offer_sdp"))
check("A4 viewer mark still opens only after setLocalDescription",
      src.index("stream_viewer_open(provider, instance_name)")
      > src.index("await pc.setLocalDescription(answer)"))


# --- B..E. each failure point: immediate cleanup ------------------------------
def failed_offer_case(label, setup):
    fresh()
    setup()
    try:
        offer("wf", "local")
        raised = False
        exc = None
    except Exception as e:  # noqa: BLE001 - the point is that it propagates
        raised = True
        exc = e
    pc = FakePC.instances[-1] if FakePC.instances else None
    check(f"{label}: exception propagates", raised, repr(exc))
    check(f"{label}: pc closed immediately (no 180s wait)",
          pc is not None and pc.closed is True)
    check(f"{label}: _webrtc_pcs entry popped", len(app._webrtc_pcs) == 0)
    check(f"{label}: no viewer mark opened",
          ("local", "wf") not in app._stream_viewers
          and marker("wf") is None)


failed_offer_case("B sdp-parse",
                   lambda: setattr(_FakeSDP, "fail_parse", True))
failed_offer_case("C setRemoteDescription",
                   lambda: setattr(FakePC, "fail_at", "setRemoteDescription"))
failed_offer_case("D createAnswer",
                   lambda: setattr(FakePC, "fail_at", "createAnswer"))
failed_offer_case("E setLocalDescription",
                   lambda: setattr(FakePC, "fail_at", "setLocalDescription"))

# --- F. registry untouched by the failure path --------------------------------
fresh()
FakePC.fail_at = "setRemoteDescription"
try:
    offer("wf6", "local")
except RuntimeError:
    pass
check("F1 stream_viewers untouched by failed handshake",
      app._stream_viewers == {})
check("F2 no marker file from failed handshake", marker("wf6") is None)

# --- G. happy path unchanged --------------------------------------------------
fresh()
sdp, pc = offer("wg", "local")
check("G1 happy path returns an answer", isinstance(sdp, str) and sdp)
check("G2 happy path: pc NOT closed", pc.closed is False)
check("G3 happy path: registry holds the entry", len(app._webrtc_pcs) == 1)
check("G4 happy path: viewer mark opened",
      app._stream_viewers.get(("local", "wg")) == 1
      and marker("wg") is not None)

# --- H. stale sweep after cleanup is a no-op ----------------------------------
fresh()
FakePC.fail_at = "createAnswer"
try:
    offer("wh", "local")
except RuntimeError:
    pass
pc = FakePC.instances[-1]
# The sweep was scheduled before the handshake failed; drive it manually.
sweep_state = pc.connectionState
check("H1 failed pc is closed, sweep sees no new/connecting",
      sweep_state == "closed")
# Simulate what _stale_sweep does: only new/connecting triggers a close.
if sweep_state in ("new", "connecting"):
    fut = asyncio.run_coroutine_threadsafe(
        pc.close(), LOOP)
    fut.result(timeout=5)
check("H2 registry still empty after sweep window", len(app._webrtc_pcs) == 0)
check("H3 pc still closed", pc.closed is True)
check("H4 no viewer mark resurrected",
      ("local", "wh") not in app._stream_viewers and marker("wh") is None)

# --- I. idempotent double-close on the failure path ---------------------------
fresh()
FakePC.fail_at = "setLocalDescription"
try:
    offer("wi", "local")
except RuntimeError:
    pass
pc = FakePC.instances[-1]
check("I1 first cleanup closed the pc", pc.closed is True)
try:
    fut = asyncio.run_coroutine_threadsafe(pc.close(), LOOP)
    fut.result(timeout=5)
    double_ok = True
except Exception as e:  # noqa: BLE001
    double_ok = False
    print(f"  note: double close raised {e!r}")
check("I2 second close harmless", double_ok)
check("I3 registry still empty", len(app._webrtc_pcs) == 0)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
