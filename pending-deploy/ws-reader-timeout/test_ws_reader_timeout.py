"""Unit tests for the ws-reader-timeout fix (manager/app.py).

Gap: both stream sockets (MJPEG _handle_ws, H.264 _handle_ws_h264) run
with sock.settimeout(10), and both reader threads treated ANY ws_read_frame
result of (None, None) as "client closed". But socket.timeout is a subclass
of OSError, so an idle recv timeout fell into ws_read_frame's OSError
handler and returned (None, None) — indistinguishable from a real close.
The dashboard client never sends frames on these sockets (input goes over
the WebRTC data channel; the MJPEG/H.264 sockets are recv-only), so EVERY
MJPEG/H.264 viewer was killed ~10s after opening and then reconnected in
a loop (MJPEG 3s retry; H.264 fail -> MJPEG). The 10s timeout exists so the
reader wakes periodically and can notice `stop` — it was never meant to
mean "closed".

Fix: ws_read_frame catches socket.timeout BEFORE OSError and returns the
WS_READ_TIMEOUT (-1) sentinel; both readers `continue` on it (keep waiting)
and only stop on None (close/error) or opcode 0x8. The timeout wake-up is
preserved: a reader still notices an externally-set stop within one
timeout interval.

Run: python3 test_ws_reader_timeout.py

Hermetic: the real app.py module is loaded; all socket work uses local
socketpairs. No docker, no network, no real files.
"""
import importlib.util
import os
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
HTML = os.path.join(REPO, "manager", "static", "index.html")

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


def load_app():
    spec = importlib.util.spec_from_file_location("wsrt_app", APP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wsrt_app"] = mod
    spec.loader.exec_module(mod)
    return mod


def client_frame(opcode, payload):
    """One masked client->server ws frame (browsers always mask)."""
    mask = b"\x01\x02\x03\x04"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    ln = len(payload)
    if ln < 126:
        hdr = bytes([0x80 | opcode, 0x80 | ln])
    elif ln < 65536:
        hdr = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", ln)
    else:
        hdr = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", ln)
    return hdr + mask + masked


def run_reader(app, sock, stop, iters_out):
    """Mirror of the patched reader loops in _handle_ws/_handle_ws_h264."""
    n = 0
    while not stop.is_set():
        op, _ = app.ws_read_frame(sock)
        n += 1
        if op == app.WS_READ_TIMEOUT:
            continue
        if op is None or op == 0x8:
            stop.set()
            break
        if n > 10000:
            break
    iters_out.append(n)


def main():
    try:
        app = load_app()
    except Exception as e:  # noqa: BLE001 - report, don't crash the suite
        bad("app module loads", repr(e))
        print(f"PASS={PASS} FAIL={FAIL}")
        return 1
    with open(APP) as f:
        src = f.read()
    with open(HTML) as f:
        html = f.read()

    # ---- A: constants and the subclass fact that caused the bug ----
    check("sentinel is -1", app.WS_READ_TIMEOUT == -1)
    check("sentinel is not the close opcode",
          app.WS_READ_TIMEOUT != 0x8)
    check("sentinel is not None", app.WS_READ_TIMEOUT is not None)
    check("socket.timeout is an OSError subclass (old handler swallowed it)",
          issubclass(socket.timeout, OSError))

    # ---- B: source contracts ----
    check("ws_read_frame catches socket.timeout",
          "except socket.timeout:" in src)
    check("timeout handler precedes the OSError handler",
          src.index("except socket.timeout:") <
          src.index("except (OSError, struct.error):"))
    check("exactly two reader timeout-continue sites",
          src.count("if op == WS_READ_TIMEOUT:") == 2)
    check("exactly two ws_read_frame call sites (MJPEG + H.264)",
          src.count("op, _ = ws_read_frame(sock)") == 2)
    check("both stream handlers keep the 10s wake-up timeout",
          src.count("sock.settimeout(10)") == 2)
    check("MJPEG reader site lives before the H.264 handler",
          src.count("if op == WS_READ_TIMEOUT:") == 2 and
          src.index("if op == WS_READ_TIMEOUT:") <
          src.index("def _handle_ws_h264"))
    check("dashboard never sends on the MJPEG socket",
          "mjpegWs.send" not in html)
    check("dashboard never sends on the H.264 socket",
          "h264Ws.send" not in html)

    # ---- C: real ws_read_frame over socketpairs ----
    srv, cli = socket.socketpair()
    try:
        srv.settimeout(0.05)
        for i in range(3):
            check(f"idle recv timeout {i + 1} returns the sentinel, not None",
                  app.ws_read_frame(srv) == (app.WS_READ_TIMEOUT, None))
        cli.sendall(client_frame(0x1, b"hello"))
        op, payload = app.ws_read_frame(srv)
        check("masked text frame opcode", op == 0x1, repr(op))
        check("masked text frame payload", payload == b"hello", repr(payload))
        cli.sendall(client_frame(0x2, bytes(range(200))))
        op, payload = app.ws_read_frame(srv)
        check("extended-length binary frame round-trips",
              op == 0x2 and payload == bytes(range(200)))
        cli.sendall(client_frame(0x8, b""))
        check("close frame yields opcode 0x8",
              app.ws_read_frame(srv) == (0x8, b""))
    finally:
        srv.close()
        cli.close()

    srv, cli = socket.socketpair()
    try:
        srv.settimeout(0.05)
        cli.close()
        check("peer socket close yields (None, None)",
              app.ws_read_frame(srv) == (None, None))
    finally:
        srv.close()

    srv, _cli = socket.socketpair()
    try:
        srv.settimeout(0.05)
        srv.close()
        check("recv on a locally-closed socket yields (None, None)",
              app.ws_read_frame(srv) == (None, None))
    finally:
        try:
            _cli.close()
        except Exception:
            pass

    # ---- D: reader-loop behaviour (mirrors of the patched readers) ----
    srv, cli = socket.socketpair()
    try:
        srv.settimeout(0.05)
        stop = threading.Event()
        iters = []
        t = threading.Thread(target=run_reader,
                             args=(app, srv, stop, iters), daemon=True)
        t.start()
        time.sleep(0.35)  # several idle timeouts must NOT stop the loop
        check("reader still alive after several idle timeouts",
              t.is_alive() and not stop.is_set())
        cli.sendall(client_frame(0x8, b""))
        t.join(timeout=2.0)
        check("reader stops on a real close frame",
              not t.is_alive() and stop.is_set())
        check("reader survived timeouts before the close",
              iters and iters[0] >= 3, repr(iters))
    finally:
        srv.close()
        cli.close()

    srv, cli = socket.socketpair()
    try:
        srv.settimeout(0.05)
        stop = threading.Event()
        iters = []
        t = threading.Thread(target=run_reader,
                             args=(app, srv, stop, iters), daemon=True)
        t.start()
        time.sleep(0.2)
        cli.sendall(client_frame(0x1, b"ping-text"))
        time.sleep(0.2)
        check("reader ignores a non-close frame and keeps waiting",
              t.is_alive() and not stop.is_set())
        cli.sendall(client_frame(0x8, b""))
        t.join(timeout=2.0)
        check("reader stops on the close frame after a data frame",
              not t.is_alive() and stop.is_set())
    finally:
        srv.close()
        cli.close()

    srv, cli = socket.socketpair()
    try:
        srv.settimeout(0.05)
        stop = threading.Event()
        iters = []
        t = threading.Thread(target=run_reader,
                             args=(app, srv, stop, iters), daemon=True)
        t0 = time.time()
        t.start()
        time.sleep(0.2)
        stop.set()  # external stop (frameless guard / socket close path)
        t.join(timeout=2.0)
        dt = time.time() - t0
        check("externally-set stop is noticed promptly via timeout wake-up",
              not t.is_alive() and dt < 2.0, f"{dt:.2f}s")
    finally:
        srv.close()
        cli.close()

    srv, cli = socket.socketpair()
    try:
        srv.settimeout(0.05)
        stop = threading.Event()
        iters = []
        t = threading.Thread(target=run_reader,
                             args=(app, srv, stop, iters), daemon=True)
        t.start()
        time.sleep(0.2)
        cli.close()  # peer vanishes: (None, None) must still stop the loop
        t.join(timeout=2.0)
        check("reader stops when the peer socket closes",
              not t.is_alive() and stop.is_set())
    finally:
        srv.close()

    print(f"PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
