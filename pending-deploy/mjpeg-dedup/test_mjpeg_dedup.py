#!/usr/bin/env python3
"""mjpeg-dedup: send-on-change MJPEG + keepalive ping.

Gap: the MJPEG WebSocket handler (_handle_ws) pushed a full JPEG at 20Hz
per viewer even when the screen was byte-identical — the local read was
already mtime-cached, but the *send* was not. A static screen (boot,
loading, idle game) cost ~20 identical JPEGs/sec/viewer of bandwidth for
nothing. (With adaptive-screencap, an idle instance's loop is 2s, so an
idle viewer got each frame re-sent ~40 times.)

Fix (manager/app.py):
  * _handle_ws only ws_send_binary()s when the frame differs from the
    last one sent (bytes compare; first frame always sent).
  * While the source is alive it also sends a tiny text "ping" every
    WS_PING_SECS (1.0s) via the new ws_send_text(). The ping keeps the
    dashboard's 3s stall badge honest (a real stall sends nothing at
    all) and stops idle proxies/NATs dropping the socket. No pings are
    sent while the source is dead — a stall must show as "congelada".
  * The 15s frameless guard is untouched: it still trips on a dead
    source (get_frame() -> None), and its clock still updates on every
    *fetched* frame, changed or not.

Dashboard (static/index.html): startMJPEG's onmessage still calls
noteFrame() on every message, but returns early for string payloads so
the keepalive never touches the <img>. The stream-freshness block itself
is unchanged.

Hermetic: no docker, no network — the real _handle_ws is driven with
fakes, and the dashboard contract is asserted textually.
"""
import contextlib
import os
import re
import struct
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "manager"))
import app

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


# ---------------------------------------------------------------- ws_send_text
print("== ws_send_text frame format ==")


class FakeSock:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendall(self, data):
        self.sent.append(data)

    def settimeout(self, t):
        pass

    def close(self):
        self.closed = True


s = FakeSock()
app.ws_send_text(s, "ping")
check("text frame opcode 0x81", s.sent[0][0] == 0x81)
check("text payload intact", s.sent[0][2:] == b"ping")
check("short length byte", s.sent[0][1] == 4)

s2 = FakeSock()
app.ws_send_text(s2, "x" * 200)
check("extended length marker 126", s2.sent[0][1] == 126)
ln = struct.unpack(">H", s2.sent[0][2:4])[0]
check("extended length value 200", ln == 200)
check("extended payload intact", s2.sent[0][4:] == b"x" * 200)

s3 = FakeSock()
app.ws_send_text(s3, "")
check("empty text frame", s3.sent[0] == bytes([0x81, 0x00]))

# ---------------------------------------------------------------- _handle_ws
print("== manager _handle_ws send-on-change + ping ==")


def run_handler(shot_fn, frameless=15, ping_secs=0.2, close_after=None,
                probe_at=None, send_binary=None, send_text=None,
                join_timeout=6):
    """Drive the real _handle_ws with fakes. Returns (sock, sends, prints,
    alive, alive_probe) where sends = [("bin"|"txt", payload), ...]."""
    client_open = threading.Event()
    client_open.set()

    def fake_ws_read_frame(sock):
        while client_open.is_set():
            time.sleep(0.05)
        return (0x8, None)

    sends = []
    prints = []
    tmpbase = os.path.join("/tmp", "mjpeg-dedup-test-%d" % os.getpid())
    h = app.Handler.__new__(app.Handler)
    h.headers = {}
    sock = FakeSock()
    h.request = sock
    h.path = "/ws/screen/t1?token=x"
    h._authed_ws = lambda: True
    h._send = lambda code, body: None

    def rec_bin(s, d):
        if send_binary is not None:
            send_binary(s, d)
        sends.append(("bin", d))

    def rec_txt(s, t):
        if send_text is not None:
            send_text(s, t)
        sends.append(("txt", t))

    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(app, "load_instances",
                                          lambda: {"t1": {"provider": "local"}}))
    stack.enter_context(mock.patch.object(app, "container_status",
                                          lambda c: "running"))
    stack.enter_context(mock.patch.object(app, "ws_handshake",
                                          lambda hdrs: b""))
    stack.enter_context(mock.patch.object(app, "ws_read_frame",
                                          fake_ws_read_frame))
    stack.enter_context(mock.patch.object(app, "ws_send_binary", rec_bin))
    stack.enter_context(mock.patch.object(app, "ws_send_text", rec_txt))
    stack.enter_context(mock.patch.object(app, "capture_screenshot", shot_fn))
    stack.enter_context(mock.patch.object(app, "WS_FRAMELESS_TIMEOUT",
                                          frameless))
    stack.enter_context(mock.patch.object(app, "WS_PING_SECS", ping_secs))
    stack.enter_context(mock.patch.object(app, "_STREAM_WANT_BASE", tmpbase))
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
    t.join(timeout=join_timeout)
    alive = t.is_alive()
    client_open.clear()
    t.join(timeout=3)
    stack.close()
    return sock, sends, prints, alive, alive_probe


def bins(sends):
    return [p for k, p in sends if k == "bin"]


def txts(sends):
    return [p for k, p in sends if k == "txt"]


# 1. identical frames -> exactly one binary send; pings keep flowing
sock, sends, prints, alive, _ = run_handler(
    lambda name: b"J" * 4000, frameless=15, ping_secs=0.25,
    close_after=1.2, join_timeout=6)
check("static screen: exactly one binary send", len(bins(sends)) == 1)
check("static screen: pings flow (~1 per 0.25s)",
      len(txts(sends)) >= 3)
check("static screen: every ping is 'ping'",
      all(p == "ping" for p in txts(sends)))
check("static screen: client close ends the loop", not alive)
check("static screen: socket closed on exit", sock.closed)
check("static screen: no guard log while source alive",
      not any("[ws] mjpeg" in p for p in prints))

# 2. changed frame -> re-sent (A x N, then B x N -> 2 binary sends)
calls = {"n": 0}


def shot_ab(name):
    calls["n"] += 1
    return b"A" * 2000 if calls["n"] <= 8 else b"B" * 2000


sock2, sends2, _, alive2, _ = run_handler(
    shot_ab, frameless=15, ping_secs=10, close_after=1.2, join_timeout=6)
b2 = bins(sends2)
check("changed frame: two binary sends", len(b2) == 2)
check("changed frame: first is A, second is B",
      len(b2) == 2 and b2[0] == b"A" * 2000 and b2[1] == b"B" * 2000)
check("changed frame: no premature ping (interval 10s)",
      txts(sends2) == [])
check("changed frame: loop still healthy", not alive2)

# 3. dead source -> frameless guard trips, and NO pings during the stall
#    (the dashboard must show "congelada", not a fake-alive stream)
sock3, sends3, prints3, alive3, _ = run_handler(
    lambda name: None, frameless=0.4, ping_secs=0.2,
    close_after=None, join_timeout=6)
check("dead source: guard still trips", not alive3)
check("dead source: guard logs once",
      sum(1 for p in prints3 if "[ws] mjpeg t1" in p and "no frames" in p) == 1)
check("dead source: zero binary sends", bins(sends3) == [])
check("dead source: zero pings during stall (honest badge)",
      txts(sends3) == [])

# 4. OSError on send -> loop breaks cleanly, socket closed via finally
def boom(s, d):
    raise OSError("broken pipe")


sock4, sends4, _, alive4, _ = run_handler(
    lambda name: b"J" * 4000, frameless=15, ping_secs=10,
    close_after=None, send_binary=boom, join_timeout=6)
check("send failure: loop exits", not alive4)
check("send failure: socket closed", sock4.closed)

# 5. production default is 1.0s (contract on the module source)
src = open(os.path.join(os.path.dirname(__file__), "..", "..", "manager",
                        "app.py")).read()
check("WS_PING_SECS default is 1.0",
      "WS_PING_SECS = 1.0" in src)
check("ws_send_text defined", "def ws_send_text(sock, text):" in src)
check("dedup compares against last sent",
      "if frame != last_sent:" in src)

# ---------------------------------------------------------------- dashboard
print("== dashboard startMJPEG contract ==")

idx = open(os.path.join(os.path.dirname(__file__), "..", "..", "manager",
                        "static", "index.html")).read()


def block_between(src, start, end):
    i = src.find(start)
    if i < 0:
        return ""
    j = src.find(end, i + len(start))
    return src[i:j] if j > i else src[i:]


mjpeg = block_between(idx, "function startMJPEG(name){",
                      "// H.264-over-WebSocket") or ""
check("MJPEG onmessage notes frame (freshness contract)",
      "mjpegWs.onmessage" in mjpeg and "noteFrame();" in mjpeg)
check("MJPEG onmessage ignores string pings for the image",
      "typeof ev.data === 'string'" in mjpeg)
# the string guard must come before the Blob construction
gi = mjpeg.find("typeof ev.data === 'string'")
bi = mjpeg.find("new Blob(")
check("ping guard precedes image update", 0 < gi < bi)
check("noteFrame still fires for pings (before the guard)",
      mjpeg.find("noteFrame();") < gi)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
