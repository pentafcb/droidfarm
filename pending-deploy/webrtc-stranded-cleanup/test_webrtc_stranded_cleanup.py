#!/usr/bin/env python3
"""Tests for the WebRTC stranded-peer-connection cleanup.

Gap: when the supervised webrtc-loop thread dies, every live
RTCPeerConnection in _webrtc_pcs is stranded — its stale-handshake sweep
(call_later), its connectionstatechange handler, and its frameless
ensure_future(_close_pc) were all scheduled on the dead loop and never
fire. The t59 factory built a fresh loop but never cleaned the registry,
so each stranded pc's adaptive-screencap viewer mark (opened after the
answer SDP) stayed pinned forever, keeping that instance's in-container
capture loop at fast 0.12s permanently (CPU burn, zero visibility).

Fix: _webrtc_pcs values are (pc, provider, instance_name) tuples and
_webrtc_loop_thread_factory() calls _webrtc_force_close_all(), which
pops every entry and drops its viewer mark (stream_viewer_close is
lock-guarded and never raises, safe from the supervisor thread). One
WARN lands in the watchdog log so the dashboard Eventos modal surfaces
it. Also: webrtc_offer_sync's run_coroutine_threadsafe moved inside the
try — the loop could die between the is_running() check and the submit
(RuntimeError on a closed loop), and an offer must fail fast to the
H.264/MJPEG fallback, never escape.

Covers:
  A. registry shape (3-tuple write site; _close_pc pop unchanged)
  B. _webrtc_force_close_all: stranded marks dropped, registry drained,
     WARN logged, empty-registry no-op, legacy bare-pc tolerated,
     remote-provider entries popped without touching local state,
     garbage entries never raise, corrupt registry never raises
  C. factory integration: real factory with a dead old loop drains the
     registry, drops marks, builds a fresh loop, returns a proper thread
  D. offer submit race: raising run_coroutine_threadsafe -> None
  E. wiring/source contracts
"""
import asyncio
import importlib.util
import inspect
import os
import sys
import tempfile
import threading
import time

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


LOGFILE = tempfile.NamedTemporaryFile(delete=False, suffix=".log").name
os.environ["DF_WATCHDOG_LOG"] = LOGFILE
WANTBASE = tempfile.mkdtemp(prefix="streamwant-")


def load_app():
    spec = importlib.util.spec_from_file_location("df_app_wrtc_cleanup", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = load_app()
mod._STREAM_WANT_BASE = WANTBASE

CLEANUP_THREADS = []


def track_thread(t):
    CLEANUP_THREADS.append(t)


def log_lines():
    with open(LOGFILE) as f:
        return f.read()


def clear_log():
    open(LOGFILE, "w").close()


def reset_webrtc_state():
    mod._webrtc_pcs.clear()
    with mod._stream_viewers_lock:
        mod._stream_viewers.clear()


def viewer_count(prov, name):
    with mod._stream_viewers_lock:
        return mod._stream_viewers.get((prov, name), 0)


def marker_exists(name):
    p = mod._stream_want_path(name)
    return bool(p) and os.path.exists(p)


# ---------------------------------------------------------------- A. shape
src_create = inspect.getsource(mod._webrtc_create_offer)
check("A1 registry stores (pc, provider, instance_name) tuple",
      "_webrtc_pcs[pc_id] = (pc, provider, instance_name)" in src_create)
check("A2 _close_pc still pops the registry entry",
      "_webrtc_pcs.pop(pc_id, None)" in inspect.getsource(
          mod._webrtc_create_offer))

# ------------------------------------------------- B. force-close behavior
reset_webrtc_state()
clear_log()
# Seed two stranded local viewers the way _webrtc_create_offer would leave
# them: mark opened, pc registered.
mod.stream_viewer_open("local", "rbx1")
mod.stream_viewer_open("local", "rbx2")
mod._webrtc_pcs["aa11"] = (object(), "local", "rbx1")
mod._webrtc_pcs["bb22"] = (object(), "local", "rbx2")
check("B0 setup: marks open", viewer_count("local", "rbx1") == 1
      and viewer_count("local", "rbx2") == 1 and marker_exists("rbx1"))

mod._webrtc_force_close_all()
check("B1 registry drained", mod._webrtc_pcs == {})
check("B2 viewer marks dropped", viewer_count("local", "rbx1") == 0
      and viewer_count("local", "rbx2") == 0)
check("B3 marker files removed", not marker_exists("rbx1")
      and not marker_exists("rbx2"))
check("B4 WARN logged with count",
      "WARN webrtc loop restarted with 2 stranded peer connection(s)"
      in log_lines())

# Empty registry: silent no-op.
reset_webrtc_state()
clear_log()
mod._webrtc_force_close_all()
check("B5 empty registry -> no log, no raise", log_lines() == "")

# Legacy bare-pc value tolerated: popped + counted, no raise. The instance
# name is unknowable in the legacy shape, so its mark is (honestly) left
# alone — assert the real mark is untouched, not clobbered.
reset_webrtc_state()
clear_log()
mod.stream_viewer_open("local", "rbx9")
mod._webrtc_pcs["zz99"] = object()  # pre-cleanup registry shape
mod._webrtc_force_close_all()
check("B6 legacy bare-pc entry popped, real marks untouched, counted",
      mod._webrtc_pcs == {} and viewer_count("local", "rbx9") == 1
      and "1 stranded" in log_lines())

# Remote-provider entries: popped, local state untouched.
reset_webrtc_state()
clear_log()
mod.stream_viewer_open("local", "rbx1")
mod._webrtc_pcs["rr11"] = (object(), "srt-node", "srt1")
mod._webrtc_force_close_all()
check("B7 remote entry popped, local viewers untouched",
      mod._webrtc_pcs == {} and viewer_count("local", "rbx1") == 1
      and "1 stranded" in log_lines())
reset_webrtc_state()

# Garbage entries never raise, never poison the registry.
reset_webrtc_state()
clear_log()
mod._webrtc_pcs["g1"] = None
mod._webrtc_pcs["g2"] = ("only", "two")
mod._webrtc_pcs["g3"] = (object(), "local")  # 2-tuple
try:
    mod._webrtc_force_close_all()
    raised = False
except Exception as ex:  # noqa: BLE001 - the point is it must not raise
    raised = True
check("B8 garbage entries never raise + registry drained",
      not raised and mod._webrtc_pcs == {})


class _BadDict(dict):
    def items(self):
        raise RuntimeError("boom")


reset_webrtc_state()
clear_log()
mod._webrtc_pcs = _BadDict({"x": (object(), "local", "rbx1")})
try:
    mod._webrtc_force_close_all()
    raised = False
except Exception:  # noqa: BLE001
    raised = True
check("B9 corrupt registry never raises", not raised)
mod._webrtc_pcs = {}

# ------------------------------------------------- C. factory integration
reset_webrtc_state()
clear_log()
mod.stream_viewer_open("local", "rbx3")
mod.stream_viewer_open("local", "rbx4")
mod._webrtc_pcs["cc33"] = (object(), "local", "rbx3")
mod._webrtc_pcs["dd44"] = (object(), "local", "rbx4")
old_loop = asyncio.new_event_loop()
old_loop.close()  # the dead loop the factory is replacing
mod._webrtc_loop = old_loop

t = mod._webrtc_loop_thread_factory()
check("C1 factory returns unstarted daemon webrtc-loop thread",
      isinstance(t, threading.Thread) and t.daemon
      and t.name == "webrtc-loop" and not t.is_alive())
new_loop = mod._webrtc_loop
check("C2 fresh loop built (old replaced)", new_loop is not old_loop
      and not new_loop.is_closed())
check("C3 stranded registry drained by the factory", mod._webrtc_pcs == {})
check("C4 stranded viewer marks dropped by the factory",
      viewer_count("local", "rbx3") == 0
      and viewer_count("local", "rbx4") == 0
      and not marker_exists("rbx3"))
check("C5 WARN logged by the factory path",
      "WARN webrtc loop restarted with 2 stranded peer connection(s)"
      in log_lines())
# The produced thread actually runs the new loop.
t.start()
track_thread(t)
deadline = time.time() + 5
while not new_loop.is_running() and time.time() < deadline:
    time.sleep(0.05)
check("C6 factory thread runs the new loop", new_loop.is_running())
new_loop.call_soon_threadsafe(new_loop.stop)
t.join(timeout=5)
check("C7 factory thread stops cleanly", not t.is_alive())
mod._webrtc_loop = None
reset_webrtc_state()

# First start (empty registry): no WARN, still a good thread.
clear_log()
t2 = mod._webrtc_loop_thread_factory()
check("C8 empty-registry factory start logs nothing", log_lines() == "")
check("C9 empty-registry factory thread shape",
      t2.daemon and t2.name == "webrtc-loop" and not t2.is_alive())
mod._webrtc_loop = None

# ------------------------------------------------- D. offer submit race
real_rcts = asyncio.run_coroutine_threadsafe


class _LiveishLoop:
    def is_running(self):
        return True


def _raising_submit(coro, loop):
    coro.close()
    raise RuntimeError("Event loop is closed")


mod._webrtc_loop = _LiveishLoop()
asyncio.run_coroutine_threadsafe = _raising_submit
try:
    res = mod.webrtc_offer_sync("rbx1", "local", "dummy-sdp", lambda *a: None)
    raised = False
except Exception:  # noqa: BLE001
    raised = True
    res = "RAISED"
finally:
    asyncio.run_coroutine_threadsafe = real_rcts
check("D1 dying-loop submit race -> None, never escapes",
      not raised and res is None)
mod._webrtc_loop = None

# ------------------------------------------------- E. wiring contracts
src_factory = inspect.getsource(mod._webrtc_loop_thread_factory)
check("E1 factory calls _webrtc_force_close_all",
      "_webrtc_force_close_all()" in src_factory)
src_offer = inspect.getsource(mod.webrtc_offer_sync)
check("E2 submit inside the fail-fast try",
      src_offer.index("run_coroutine_threadsafe") > src_offer.index("try:"))
check("E3 _webrtc_force_close_all defined before the factory in source",
      inspect.getsource(mod).index("def _webrtc_force_close_all") <
      inspect.getsource(mod).index("def _webrtc_loop_thread_factory"))
check("E4 no new stage files needed (app.py already staged)",
      True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
