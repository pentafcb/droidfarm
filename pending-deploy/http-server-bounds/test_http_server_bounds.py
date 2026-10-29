#!/usr/bin/env python3
"""Tests for BoundedHTTPServer (manager/app.py + wing/agent.py).

GAP: stock ThreadingHTTPServer spawns one thread per connection with no cap
and no per-connection timeout — half-open / dribble-feed clients pin handler
threads forever and starve the whole server (fast 200 TTFB then mid-body
drops, the wedge observed 2026-10-11). BoundedHTTPServer caps concurrent
handler threads (excess connections fail fast with 503) and drops idle
connections after the socket timeout.

Covers:
  A. class exists in both files, subclasses ThreadingHTTPServer, overrides
     process_request (no unbounded super().process_request path)
  B. constants sane: HTTP_MAX_THREADS=64, HTTP_CONN_TIMEOUT=60, backlog 128
  C. main() in both files constructs BoundedHTTPServer
  D. agent VERSION 1.0.8; manager/agent classes structurally match
  E. live: saturation -> fast 503, cap never exceeded, slots released after
  F. live: idle half-open connection dropped after conn_timeout
  G. live: active stream (periodic writes) NOT cut by the timeout
  H. live: slow-dribble partial request cannot hold a slot past the timeout
  I. live: garbage request -> server stays up, slot released
  J. live: 503-path client that vanishes instantly -> server stays healthy

Hermetic: real localhost sockets only; no docker, no external network.
"""
import importlib.util
import inspect
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
AGENT = os.path.join(REPO, "wing", "agent.py")

def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

app = load("hsb_app", APP)
agent = load("hsb_agent", AGENT)

PASS, FAIL = 0, 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")

# ---------------- A. class shape ----------------
for mod, label in ((app, "manager"), (agent, "agent")):
    cls = getattr(mod, "BoundedHTTPServer", None)
    check(f"A {label}: BoundedHTTPServer exists", cls is not None)
    check(f"A {label}: subclasses ThreadingHTTPServer",
          cls is not None and issubclass(cls, ThreadingHTTPServer))
    check(f"A {label}: overrides process_request (no unbounded spawn)",
          cls is not None and "process_request" in cls.__dict__)
    src = inspect.getsource(cls.process_request) if cls else ""
    check(f"A {label}: never delegates to super().process_request",
          "super().process_request(" not in src)

# ---------------- B. constants ----------------
for mod, label in ((app, "manager"), (agent, "agent")):
    check(f"B {label}: HTTP_MAX_THREADS=64",
          getattr(mod, "HTTP_MAX_THREADS", None) == 64,
          repr(getattr(mod, "HTTP_MAX_THREADS", None)))
    check(f"B {label}: HTTP_CONN_TIMEOUT=60",
          getattr(mod, "HTTP_CONN_TIMEOUT", None) == 60,
          repr(getattr(mod, "HTTP_CONN_TIMEOUT", None)))
    check(f"B {label}: HTTP_LISTEN_BACKLOG=128",
          getattr(mod, "HTTP_LISTEN_BACKLOG", None) == 128,
          repr(getattr(mod, "HTTP_LISTEN_BACKLOG", None)))

# ---------------- C. main() wiring ----------------
check("C manager main() constructs BoundedHTTPServer",
      "BoundedHTTPServer(" in inspect.getsource(app.main))
check("C agent main() constructs BoundedHTTPServer",
      "BoundedHTTPServer(" in inspect.getsource(agent.main))
check("C manager main() no longer uses raw ThreadingHTTPServer",
      "ThreadingHTTPServer((" not in inspect.getsource(app.main))
check("C agent main() no longer uses raw ThreadingHTTPServer",
      "ThreadingHTTPServer((" not in inspect.getsource(agent.main))

# ---------------- D. version + structural parity ----------------
check("D agent VERSION is 1.0.8+ (1.0.9 adds selfwatch-events endpoint)", agent.VERSION in ("1.0.8", "1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"), repr(agent.VERSION))
am = {n for n in ("__init__", "get_request", "process_request",
                  "_process_request_guarded")
      if n in app.BoundedHTTPServer.__dict__}
gm = {n for n in ("__init__", "get_request", "process_request",
                  "_process_request_guarded")
      if n in agent.BoundedHTTPServer.__dict__}
check("D manager/agent BoundedHTTPServer structurally match", am == gm,
      f"app={sorted(am)} agent={sorted(gm)}")
for mod, label in ((app, "manager"), (agent, "agent")):
    src = inspect.getsource(mod.BoundedHTTPServer)
    check(f"D {label}: 503 fail-fast path present", "503 Service Unavailable" in src)
with open(AGENT) as f:
    agent_src = f.read()
check("D agent imports socket", re.search(r"^import socket$", agent_src, re.M) is not None)

# ---------------- live-server helpers ----------------
class CtlHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    block_event = threading.Event()
    entered = 0
    max_entered = 0
    lock = threading.Lock()
    slow_chunks = 5

    def log_message(self, *a):
        pass

    def _send(self, body):
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            pass

    def do_GET(self):
        if self.path == "/block":
            with CtlHandler.lock:
                CtlHandler.entered += 1
                CtlHandler.max_entered = max(CtlHandler.max_entered,
                                            CtlHandler.entered)
            try:
                CtlHandler.block_event.wait(timeout=15)
            finally:
                with CtlHandler.lock:
                    CtlHandler.entered -= 1
            self._send(b"unblocked")
        elif self.path == "/slow":
            # Active stream: a byte every 0.4s with conn_timeout=1 — activity
            # must keep the connection alive.
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(CtlHandler.slow_chunks))
                self.end_headers()
                for _ in range(CtlHandler.slow_chunks):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.4)
            except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                pass
        elif self.path == "/fast":
            self._send(b"fast")
        else:
            self._send(b"?")

def make_server(base_cls, **overrides):
    class S(base_cls):
        pass
    for k, v in overrides.items():
        setattr(S, k, v)
    srv = S(("127.0.0.1", 0), CtlHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

def raw_conn(srv):
    return socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)

def get_status(c, path):
    c.sendall(("GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
               % path).encode())
    return c.makefile("rb").readline().decode("latin1", "replace")

def reset_ctl():
    CtlHandler.block_event.clear()
    with CtlHandler.lock:
        CtlHandler.entered = 0
        CtlHandler.max_entered = 0

def wait_for(pred, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False

# ---------------- E. saturation -> fast 503, cap respected, slots released ----------------
srv = make_server(app.BoundedHTTPServer, max_threads=2, conn_timeout=30)
try:
    reset_ctl()
    c1, c2 = raw_conn(srv), raw_conn(srv)
    c1.sendall(b"GET /block HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    c2.sendall(b"GET /block HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    check("E both blockers occupy the 2 slots",
          wait_for(lambda: CtlHandler.entered == 2), repr(CtlHandler.entered))
    c3 = raw_conn(srv)
    t0 = time.time()
    line = get_status(c3, "/fast")
    dt = time.time() - t0
    check("E saturated server answers 503 (fail fast)", "503" in line, repr(line))
    check("E 503 is fast, not queued", dt < 3, f"{dt:.2f}s")
    check("E handler-thread cap never exceeded", CtlHandler.max_entered == 2,
          repr(CtlHandler.max_entered))
    c3.close()
    CtlHandler.block_event.set()
    for c in (c1, c2):
        try:
            c.makefile("rb").read()
        except OSError:
            pass
        c.close()
    check("E slots released after handlers finish",
          wait_for(lambda: CtlHandler.entered == 0), repr(CtlHandler.entered))
    c4 = raw_conn(srv)
    line4 = get_status(c4, "/fast")
    c4.close()
    check("E server serves 200 again after release", "200" in line4, repr(line4))
finally:
    srv.shutdown()
    srv.server_close()

# ---------------- F. idle half-open connection dropped ----------------
srv = make_server(app.BoundedHTTPServer, max_threads=4, conn_timeout=1)
try:
    c = raw_conn(srv)
    c.settimeout(6)
    t0 = time.time()
    try:
        data = c.recv(4096)
    except socket.timeout:
        data = b"TIMEOUT"
    dt = time.time() - t0
    check("F idle half-open connection dropped (EOF)", data == b"", repr(data))
    check("F drop happens near the timeout", dt < 4, f"{dt:.2f}s")
    c.close()
finally:
    srv.shutdown()
    srv.server_close()

# ---------------- G. active stream survives the timeout ----------------
srv = make_server(app.BoundedHTTPServer, max_threads=4, conn_timeout=1)
try:
    c = raw_conn(srv)
    c.settimeout(10)
    c.sendall(b"GET /slow HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    f = c.makefile("rb")
    status = f.readline()
    while True:  # consume headers up to the blank line
        h = f.readline()
        if h in (b"\r\n", b"\n", b""):
            break
    body = b""
    while len(body) < CtlHandler.slow_chunks:
        chunk = f.read(1)
        if not chunk:
            break
        body += chunk
    check("G slow stream status 200", b"200" in status, status)
    check("G active stream delivered fully despite 1s timeout",
          body == b"x" * CtlHandler.slow_chunks, repr(body))
    c.close()
finally:
    srv.shutdown()
    srv.server_close()

# ---------------- H. slow-dribble cannot hold a slot ----------------
srv = make_server(app.BoundedHTTPServer, max_threads=4, conn_timeout=1)
try:
    c = raw_conn(srv)
    c.settimeout(6)
    c.sendall(b"GET /sl")  # partial request line, then silence
    time.sleep(2.5)        # > conn_timeout: server must have dropped us
    try:
        c.sendall(b"ow HTTP/1.1\r\nHost: x\r\n\r\n")
        sent_ok = True
    except OSError:
        sent_ok = False
    try:
        rest = c.recv(4096)
    except socket.timeout:
        rest = b"TIMEOUT"
    check("H dribble-feed slot released (EOF)", rest == b"",
          f"sent_ok={sent_ok} rest={rest!r}")
    c.close()
finally:
    srv.shutdown()
    srv.server_close()

# ---------------- I. garbage request: server stays up ----------------
srv = make_server(app.BoundedHTTPServer, max_threads=4, conn_timeout=5)
try:
    c = raw_conn(srv)
    c.settimeout(5)
    c.sendall(b"\x00\xff garbage \r\n\r\n")
    try:
        c.makefile("rb").readline()
    except OSError:
        pass
    c.close()
    c2 = raw_conn(srv)
    line2 = get_status(c2, "/fast")
    c2.close()
    check("I server alive after garbage request", "200" in line2, repr(line2))
finally:
    srv.shutdown()
    srv.server_close()

# ---------------- J. vanishing 503 client: server stays healthy ----------------
srv = make_server(app.BoundedHTTPServer, max_threads=1, conn_timeout=30)
try:
    reset_ctl()
    c1 = raw_conn(srv)
    c1.sendall(b"GET /block HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    check("J blocker occupies the 1 slot",
          wait_for(lambda: CtlHandler.entered == 1), repr(CtlHandler.entered))
    c2 = raw_conn(srv)
    c2.sendall(b"GET /fast HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    c2.close()  # vanish without reading the 503
    time.sleep(0.5)
    CtlHandler.block_event.set()
    try:
        c1.makefile("rb").read()
    except OSError:
        pass
    c1.close()
    check("J slot released after vanishing client",
          wait_for(lambda: CtlHandler.entered == 0), repr(CtlHandler.entered))
    c3 = raw_conn(srv)
    line3 = get_status(c3, "/fast")
    c3.close()
    check("J server healthy after vanishing 503 client", "200" in line3, repr(line3))
finally:
    srv.shutdown()
    srv.server_close()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
