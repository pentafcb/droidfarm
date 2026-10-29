#!/usr/bin/env python3
"""Tests for the supervised WebRTC event-loop thread.

Gap: the webrtc-loop thread (started by webrtc_init()) was the only
long-lived manager background thread NOT covered by the thread
supervisor (2026-10-11). A dead loop thread left _webrtc_loop non-None
but dead, so every WebRTC offer hung on fut.result(timeout=15) — 15s
per offer, forever, with zero visibility. Now the loop thread is
registered with the supervisor (fresh loop per restart), and
webrtc_offer_sync fails fast when the loop isn't running so the
dashboard falls over to H.264/MJPEG immediately while the supervisor
restarts it.

Covers:
  A. factory contract (unstarted daemon thread named webrtc-loop; fresh
     loop every call; old loop closed; thread actually runs the loop;
     hostile/closed old loops tolerated; never raises)
  B. webrtc_init backward compat (no-op without aiortc; starts a live
     loop when HAS_AIORTC is forced)
  C. supervision: register -> first pass starts the loop; dead loop ->
     restarted on a FRESH loop (old closed); restart logged
  D. circuit breaker: persistently dying loop -> give up + one ALERT
  E. webrtc_offer_sync fail-fast: None/dead/closed/raising loop ->
     None without touching run_coroutine_threadsafe; live loop ->
     proceeds to the future
  F. main() wiring: registers webrtc-loop with the factory under
     HAS_AIORTC; no bare webrtc_init() call
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


def load_app():
    spec = importlib.util.spec_from_file_location("df_app_wloopsup", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = load_app()

CLEANUP = []  # (loop, thread) pairs to stop/join at the end


def track(loop, thread):
    CLEANUP.append((loop, thread))


def wait_until(fn, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


def stop_pair(loop, thread):
    try:
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass
    try:
        if thread is not None:
            thread.join(timeout=5)
    except Exception:
        pass
    try:
        if loop is not None and not loop.is_closed():
            loop.close()
    except Exception:
        pass


# --- A. factory contract -----------------------------------------------------
mod._webrtc_loop = None

t = mod._webrtc_loop_thread_factory()
check("A1 factory returns an unstarted threading.Thread",
      isinstance(t, threading.Thread) and not t.is_alive())
check("A2 thread is daemon and named webrtc-loop",
      t.daemon is True and t.name == "webrtc-loop")

t.start()
loop1 = mod._webrtc_loop
track(loop1, t)
check("A3 started thread runs the new event loop",
      wait_until(lambda: loop1.is_running()),
      "loop never reported running")

ran = []
loop1.call_soon_threadsafe(lambda: ran.append(1))
check("A4 loop actually executes callbacks",
      wait_until(lambda: ran == [1]))

t2 = mod._webrtc_loop_thread_factory()
check("A5 second factory call does not close a still-running loop",
      not loop1.is_closed(),
      "a running loop must never be closed under its thread")
# clean scenario for the close path: start a loop, stop its thread, then
# the factory must close it. (This mirrors the real supervised-restart
# path: the factory only ever runs when the registered thread is dead.)
stop_pair(loop1, t)
mod._webrtc_loop = None
ts = mod._webrtc_loop_thread_factory()
ts.start()
loops = mod._webrtc_loop
track(loops, ts)
assert wait_until(lambda: loops.is_running()), "scenario loop did not start"
loops.call_soon_threadsafe(loops.stop)
ts.join(timeout=5)
ts2 = mod._webrtc_loop_thread_factory()
check("A5b factory closes the previous loop once its thread is dead",
      loops.is_closed(), "old loop left open")
t2 = ts2
check("A6 second factory installs a fresh loop object",
      mod._webrtc_loop is not loop1 and mod._webrtc_loop is not None)
check("A7 second factory returns an unstarted thread",
      isinstance(t2, threading.Thread) and not t2.is_alive())
t2.start()
loop2 = mod._webrtc_loop
track(loop2, t2)
check("A8 fresh loop runs after restart",
      wait_until(lambda: loop2.is_running()))
stop_pair(loop1, t)
stop_pair(loop2, t2)

# hostile old loop: is_closed raises -> factory must still work
mod._webrtc_loop = None


class HostileLoop:
    def is_closed(self):
        raise RuntimeError("boom")

    def close(self):
        raise RuntimeError("boom")


mod._webrtc_loop = HostileLoop()
try:
    t3 = mod._webrtc_loop_thread_factory()
    hostile_ok = isinstance(t3, threading.Thread)
except Exception:
    hostile_ok = False
check("A9 factory never raises on a hostile old loop", hostile_ok)
if hostile_ok:
    t3.start()
    loop3 = mod._webrtc_loop
    track(loop3, t3)
    check("A10 loop from hostile-recovery path runs",
          wait_until(lambda: loop3.is_running()))
    stop_pair(loop3, t3)

# already-closed old loop -> replaced cleanly
closed = asyncio.new_event_loop()
closed.close()
mod._webrtc_loop = closed
t4 = mod._webrtc_loop_thread_factory()
check("A11 factory replaces an already-closed loop",
      mod._webrtc_loop is not closed and not mod._webrtc_loop.is_closed())
t4.start()
loop4 = mod._webrtc_loop
track(loop4, t4)
check("A12 replacement loop runs",
      wait_until(lambda: loop4.is_running()))
stop_pair(loop4, t4)
mod._webrtc_loop = None

# --- B. webrtc_init backward compat ------------------------------------------
mod.HAS_AIORTC = False
mod._webrtc_loop = None
mod.webrtc_init()
check("B1 webrtc_init is a no-op without aiortc",
      mod._webrtc_loop is None)

mod.HAS_AIORTC = True
mod.webrtc_init()
loopb = mod._webrtc_loop
check("B2 webrtc_init starts a running loop when aiortc present",
      loopb is not None and wait_until(lambda: loopb.is_running()))


def _find_thread(loop):
    for th in threading.enumerate():
        if th.name == "webrtc-loop" and th.is_alive():
            return th
    return None


tb = _find_thread(loopb)
stop_pair(loopb, tb)
mod.HAS_AIORTC = False
mod._webrtc_loop = None
check("B3 loop thread cleaned up", tb is None or not tb.is_alive())

# --- C. supervision integration ----------------------------------------------
mod._SUPERVISED.clear()
mod.register_supervised("webrtc-loop", mod._webrtc_loop_thread_factory)
mod._supervise_once()
entry = mod._SUPERVISED["webrtc-loop"]
thr = entry.get("thread")
check("C1 first supervisor pass starts the loop thread",
      isinstance(thr, threading.Thread) and thr.is_alive())
loopc = mod._webrtc_loop
track(loopc, thr)
check("C2 supervised loop is running",
      wait_until(lambda: loopc.is_running()))

# kill it -> next pass must restart on a FRESH loop
loopc.call_soon_threadsafe(loopc.stop)
thr.join(timeout=5)
check("C3 test killed the loop thread", not thr.is_alive())
mod._supervise_once()
entry2 = mod._SUPERVISED["webrtc-loop"]
thr2 = entry2.get("thread")
loopc2 = mod._webrtc_loop
track(loopc2, thr2)
check("C4 supervisor restarted the dead loop thread",
      isinstance(thr2, threading.Thread) and thr2.is_alive()
      and thr2 is not thr)
check("C5 restart used a fresh loop, old one closed",
      loopc2 is not loopc and loopc.is_closed()
      and wait_until(lambda: loopc2.is_running()))
with open(LOGFILE) as f:
    logtxt = f.read()
check("C6 restart logged to the watchdog log",
      "webrtc-loop" in logtxt and "WARN" in logtxt)
stop_pair(loopc2, thr2)
mod._SUPERVISED.clear()
mod._webrtc_loop = None

# --- D. circuit breaker on a persistently dying loop --------------------------
mod._SUPERVISED.clear()
calls = []


def doomed_factory():
    calls.append(1)
    return threading.Thread(target=lambda: None, daemon=True,
                            name="webrtc-loop-doomed")


mod.register_supervised("webrtc-loop-doomed", doomed_factory)
for _ in range(4):
    mod._supervise_once()
    ent = mod._SUPERVISED["webrtc-loop-doomed"]
    thd = ent.get("thread")
    if thd is not None:
        thd.join(timeout=5)
entryd = mod._SUPERVISED["webrtc-loop-doomed"]
check("D1 circuit breaker gave up after 3 restarts/hour",
      entryd.get("given_up") is True, str(entryd.get("restarts")))
check("D2 factory called exactly 3 times (no restart storm)",
      len(calls) == 3, f"calls={len(calls)}")
with open(LOGFILE) as f:
    logtxt = f.read()
check("D3 single ALERT names the webrtc loop",
      logtxt.count("gave up restarting 'webrtc-loop-doomed'") == 1)
before = len(calls)
mod._supervise_once()
check("D4 no more factory calls once given up", len(calls) == before)
mod._SUPERVISED.clear()

# --- E. webrtc_offer_sync fail-fast ------------------------------------------
real_rcts = asyncio.run_coroutine_threadsafe
rcts_calls = []


def fake_rcts(coro, loop):
    rcts_calls.append((coro, loop))
    try:
        coro.close()
    except Exception:
        pass

    class F:
        def result(self, timeout=None):
            return "SDP-ANSWER"

    return F()


asyncio.run_coroutine_threadsafe = fake_rcts
try:
    mod.HAS_AIORTC = True
    mod._webrtc_loop = None
    t0 = time.time()
    r = mod.webrtc_offer_sync("rbx1", "local", "sdp", lambda *a: None)
    dt = time.time() - t0
    check("E1 None loop -> None", r is None)
    check("E2 None loop is fast (no 15s hang)", dt < 3, f"{dt:.2f}s")

    dead = asyncio.new_event_loop()  # never started
    mod._webrtc_loop = dead
    rcts_calls.clear()
    t0 = time.time()
    r = mod.webrtc_offer_sync("rbx1", "local", "sdp", lambda *a: None)
    dt = time.time() - t0
    check("E3 dead (never-started) loop -> None", r is None)
    check("E4 dead loop is fast (no 15s hang)", dt < 3, f"{dt:.2f}s")
    check("E5 dead loop never reaches run_coroutine_threadsafe",
          rcts_calls == [])
    dead.close()

    class RaisingLoop:
        def is_running(self):
            raise RuntimeError("boom")

    mod._webrtc_loop = RaisingLoop()
    rcts_calls.clear()
    try:
        r = mod.webrtc_offer_sync("rbx1", "local", "sdp", lambda *a: None)
        raising_ok, raising_val = True, r
    except Exception:
        raising_ok, raising_val = False, "raised"
    check("E6 raising is_running -> None, never propagates",
          raising_ok and raising_val is None)
    check("E7 raising loop never reaches run_coroutine_threadsafe",
          rcts_calls == [])

    # live loop -> proceeds to the future
    live = asyncio.new_event_loop()
    mod._webrtc_loop = live
    thlive = threading.Thread(
        target=lambda: (asyncio.set_event_loop(live), live.run_forever()),
        daemon=True)
    thlive.start()
    assert wait_until(lambda: live.is_running()), "live loop did not start"
    rcts_calls.clear()
    r = mod.webrtc_offer_sync("rbx1", "local", "sdp", lambda *a: None)
    check("E8 live loop proceeds to run_coroutine_threadsafe",
          r == "SDP-ANSWER" and len(rcts_calls) == 1)
    check("E9 future scheduled on the live loop",
          rcts_calls and rcts_calls[0][1] is live)
    live.call_soon_threadsafe(live.stop)
    thlive.join(timeout=5)
    live.close()
finally:
    asyncio.run_coroutine_threadsafe = real_rcts
    mod.HAS_AIORTC = False
    mod._webrtc_loop = None

# --- F. main() wiring contract ------------------------------------------------
src = inspect.getsource(mod.main)
check("F1 main registers webrtc-loop with the factory",
      '"webrtc-loop"' in src and "_webrtc_loop_thread_factory" in src
      and "register_supervised" in src)
check("F2 registration guarded by HAS_AIORTC", "if HAS_AIORTC:" in src)
check("F3 no bare webrtc_init() call in main",
      "webrtc_init()" not in src)
check("F4 factory defined before main (module level)",
      "def _webrtc_loop_thread_factory" in inspect.getsource(mod))

# --- teardown -----------------------------------------------------------------
for lp, th in CLEANUP:
    stop_pair(lp, th)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
