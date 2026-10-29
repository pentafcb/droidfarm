#!/usr/bin/env python3
"""stream-frameless-guard: frameless-loop guards on the streaming paths.

Gap: a dead frame source (dead in-container screencap loop, unreachable
wing node) made the streaming loops spin forever -
  * manager _handle_ws (MJPEG): per-viewer thread at 20Hz; for remote
    instances up to 20 failed HTTP fetches/sec against the dead node
    (fast-fail) or 10s-blocked iterations (hung node).
  * H264Broadcaster._run: 20Hz loop calling frame_source() (HTTP fetch +
    JPEG decode per tick for remote) with an idling ffmpeg encoder; the
    loop previously only stopped on subscriber-idle timeout.
Fix: both loops break after a frameless timeout (15s) with zero frames.
The dashboard recovers on its own: MJPEG onclose reconnects in 3s, and an
H264 ws close falls back to MJPEG (h264Fail).

Hermetic: no docker, no network, no ffmpeg (broadcaster driven with a
stub encoder; the MJPEG handler driven with fakes).
"""
import os
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "manager"))
import app
import h264_stream

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


# ---------------------------------------------------------------- broadcaster
class StubEnc:
    def __init__(self):
        self.writes = 0
        self.closed = False

    def write_frame(self, rgb):
        self.writes += 1
        return True

    def read_available(self, timeout=0.0):
        return b""

    def alive(self):
        return True

    def close(self):
        self.closed = True


def make_bc(source, frameless_timeout):
    bc = h264_stream.H264Broadcaster(source, width=480, height=284, fps=20,
                                     gop=20, idle_timeout=60,
                                     frameless_timeout=frameless_timeout)
    bc._stub = StubEnc()
    bc._enc = bc._stub
    bc._enc_w, bc._enc_h = 480, 284
    bc._init = b"fake-init"
    return bc


def add_sub(bc):
    sub = h264_stream.H264Subscriber(bc._init)
    bc._subs.append(sub)
    return sub


print("== H264Broadcaster frameless guard ==")

# 1. persistent frameless -> _run exits, subscribers closed, encoder reaped
bc = make_bc(lambda: None, 0.4)
sub = add_sub(bc)
t = threading.Thread(target=bc._run, daemon=True)
t.start()
t.join(timeout=5)
check("broadcaster _run exits on persistent frameless", not t.is_alive())
check("frameless trip closes subscribers", bc.subscriber_count() == 0)
check("frameless trip marks stopped", bc._stopped)
check("frameless trip reaps encoder", bc._stub.closed and bc._enc is None)
check("frameless trip closes subscriber queue", sub.read(timeout=0.1) is None)

# 2. frames flowing -> no trip
# NOTE: the source must change every tick: the broadcaster's static-skip
# (h264-skip-static) skips byte-identical frames, so a constant frame would
# legitimately encode ~1x + heartbeats, not 20fps. Changing frames exercise
# the original "frames flowing" intent.
import itertools as _it
_changing = _it.count()


def _changing_source():
    return (bytes([next(_changing) % 256]) * 480 * 284 * 3, 480, 284)


rgb = bytes(480 * 284 * 3)
bc2 = make_bc(_changing_source, 0.4)
add_sub(bc2)
t2 = threading.Thread(target=bc2._run, daemon=True)
t2.start()
time.sleep(1.0)
check("no trip while frames flow", t2.is_alive())
check("frames still encoded while flowing", bc2._stub.writes > 5)
bc2.stop()
t2.join(timeout=5)
check("stop() still works after guard change", not t2.is_alive())

# 3. frames for a while, then a stall -> the clock was being reset while
#    frames flowed (no early trip), and trips after they stop
def stall_after_flow():
    stall_after_flow.n += 1
    return (rgb, 480, 284) if stall_after_flow.n <= 10 else None  # ~0.5s flow


stall_after_flow.n = 0
bc3 = make_bc(stall_after_flow, 0.6)
add_sub(bc3)
t3 = threading.Thread(target=bc3._run, daemon=True)
t3.start()
time.sleep(0.85)  # past the no-reset deadline (~0.65s)
check("frameless clock resets while frames flow", t3.is_alive())
t3.join(timeout=5)
check("trip follows after the flow stalls", not t3.is_alive())

# 4. malformed frames do not reset the clock
bc4 = make_bc(lambda: b"junk-bytes", 0.4)
add_sub(bc4)
t4 = threading.Thread(target=bc4._run, daemon=True)
t4.start()
t4.join(timeout=5)
check("malformed frame source still trips the guard", not t4.is_alive())

# 5. production default is 15s
bc5 = h264_stream.H264Broadcaster(lambda: None)
check("broadcaster default frameless_timeout is 15s",
      bc5._frameless_timeout == 15.0)

# ---------------------------------------------------------------- MJPEG _handle_ws
print("== manager _handle_ws frameless guard ==")


class FakeSock:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.timeout = None

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, t):
        self.timeout = t

    def close(self):
        self.closed = True


def run_handler(shot_fn, frameless, run_secs, close_after=None,
                probe_at=None):
    """Drive the real _handle_ws with fakes. Returns (handler, sock, prints).

    The caller owns orchestration: the returned thread is already running;
    close the client via client_open.clear(), then join.
    """
    import contextlib
    client_open = threading.Event()
    client_open.set()

    def fake_ws_read_frame(sock):
        while client_open.is_set():
            time.sleep(0.05)
        return (0x8, None)

    prints = []
    h = app.Handler.__new__(app.Handler)
    h.headers = {}
    sock = FakeSock()
    h.request = sock
    h.path = "/ws/screen/t1?token=x"
    h._authed_ws = lambda: True
    h._send = lambda code, body: None
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(app, "load_instances",
                                          lambda: {"t1": {"provider": "local"}}))
    stack.enter_context(mock.patch.object(app, "container_status",
                                          lambda c: "running"))
    stack.enter_context(mock.patch.object(app, "ws_handshake",
                                          lambda hdrs: b""))
    stack.enter_context(mock.patch.object(app, "ws_read_frame",
                                          fake_ws_read_frame))
    stack.enter_context(mock.patch.object(app, "ws_send_binary",
                                          lambda s, d: s.sendall(b"ws:" + d)))
    stack.enter_context(mock.patch.object(app, "capture_screenshot", shot_fn))
    stack.enter_context(mock.patch.object(app, "WS_FRAMELESS_TIMEOUT",
                                          frameless))
    stack.enter_context(mock.patch("builtins.print",
                                   lambda *a, **k: prints.append(
                                       " ".join(str(x) for x in a))))
    t = threading.Thread(target=h._handle_ws, args=("t1",), daemon=True)
    t.start()
    alive_probe = None
    if probe_at is not None:
        time.sleep(probe_at)
        alive_probe = t.is_alive()
    if close_after is not None:
        time.sleep(max(0.0, close_after - (probe_at or 0.0)))
        client_open.clear()  # simulate the viewer closing
    t.join(timeout=run_secs)
    alive = t.is_alive()
    client_open.clear()
    t.join(timeout=3)
    stack.close()
    return h, sock, prints, alive, alive_probe


def frame_sends(sock):
    """Frames the handler pushed (the ws handshake also calls sendall)."""
    return [s for s in sock.sent if s.startswith(b"ws:")]


# 6. persistent frameless -> loop exits, guard logged, socket closed
h, sock, prints, alive, _ = run_handler(lambda name: None, 0.4, 5)
check("mjpeg loop exits on persistent frameless", not alive)
check("mjpeg guard logs instance + no-frames once",
      sum(1 for p in prints if "[ws] mjpeg t1" in p and "no frames" in p) == 1)
check("mjpeg guard closes the socket", sock.closed)
check("mjpeg guard sent no frames", frame_sends(sock) == [])

# 7. frames flowing -> no trip; client close still ends the loop cleanly
# (frames change each tick so send-on-change delivers every one)
calls7 = {"n": 0}


def shot_changing(name):
    calls7["n"] += 1
    return b"jpegbytes-%d" % calls7["n"]


h2, sock2, prints2, alive2, probe2 = run_handler(
    shot_changing, 0.4, 5, close_after=1.2, probe_at=0.9)
check("no trip while mjpeg frames flow", probe2)
check("mjpeg frames were delivered", len(frame_sends(sock2)) >= 5)
check("no guard log while frames flow",
      not any("[ws] mjpeg" in p for p in prints2))
check("client close still ends a healthy loop", not alive2)

# 8. intermittent frames reset the clock, then trip when they stop
calls2 = {"n": 0}


def shot_intermittent(name):
    calls2["n"] += 1
    # changing frames so send-on-change delivers each one (~0.25s of frames)
    return b"jpegbytes-%d" % calls2["n"] if calls2["n"] <= 5 else None


h3, sock3, prints3, alive3, _ = run_handler(shot_intermittent, 0.4, 5)
check("mjpeg loop trips after intermittent frames stop", not alive3)
check("intermittent frames were delivered before the trip",
      len(frame_sends(sock3)) >= 5)
check("mjpeg guard logged after the stall",
      any("[ws] mjpeg t1" in p for p in prints3))

# 9. production default is 15s (contract on the module source)
src = open(os.path.join(os.path.dirname(__file__), "..", "..", "manager",
                        "app.py")).read()
check("WS_FRAMELESS_TIMEOUT default is 15",
      "WS_FRAMELESS_TIMEOUT = 15" in src)

print(f"\n{ PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
