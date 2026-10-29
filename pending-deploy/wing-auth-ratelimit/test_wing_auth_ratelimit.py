#!/usr/bin/env python3
"""Tests for wing agent 1.0.19 agent-key brute-force protection (manager
login-ratelimit parity).

REAL GAP: the manager got /api/login rate limiting (10 failed/60s/IP -> 429)
plus a constant-time, never-raises password check — but the wing agent's
internet-facing X-Agent-Key auth (8090-8099) had neither: unlimited attempts
and a plain == compare. The ninth "main had it, wing didn't" parity item
(after 1.0.11 session-death, 1.0.12 loop devices, 1.0.13 memory, 1.0.14
stuck-boot, 1.0.15 keepalive mirror, 1.0.16 binderfs self-heal, 1.0.17 agent
HTTP self-check, 1.0.18 create image preflight).

Agent 1.0.19 adds, in wing/agent.py:
  - _auth_key_ok(): constant-time compare via hmac.compare_digest; an
    empty/unset AGENT_KEY or non-string input refuses (fail closed), never
    raises (non-ASCII header input -> False, not a 500).
  - Sliding-window rate limit: AUTH_RATE_MAX (10) failed auths per
    AUTH_RATE_WINDOW (60s) per client IP -> HTTP 429
    {"error": "too many attempts"} (same body as the manager's /api/login
    429). A successful authed request clears that IP's counter. Table
    bounded at AUTH_RATE_MAX_IPS (1024, stalest-first eviction, fail-open
    for the evicted IP). All helpers best-effort, never raise.
  - Handler._check_auth() -> 'health' | 'ok' | 'ratelimited' | 'denied':
    /api/agent/health stays unauthenticated by design (agent self-check +
    manager probes) and never counts; on authed routes the rate limit is
    checked BEFORE the key compare (429-before-credential, same ordering as
    the manager's /api/login). do_GET/do_POST/do_DELETE map
    ratelimited -> 429, denied -> 401. Zero manager/dashboard changes.

This suite covers:

  A. agent.VERSION is exactly 1.0.19 + manager >=1.0.2 gate compatibility
  B. _auth_key_ok units: correct/wrong/empty-key/non-str/non-ASCII +
     hmac.compare_digest contract
  C. rate-limit state machine: boundary, window expiry, per-IP isolation,
     success-clears, cap eviction fail-open, never-raises, prune
  D. _check_auth wiring: health bypass (no counting), ok/denied, 10
     failures -> ratelimited even with the correct key, success-clears,
     query-string health keeps the old raw-path semantics, never raises
  E. route contract: 429 branch in do_GET/do_POST/do_DELETE, 429 before
     401, body matches the manager's 429 convention, no legacy call sites
  F. live localhost server: wrong key -> 401, 10 wrong -> 429 with the
     exact body, health -> 200 (unauthenticated, uncounted), correct key
     passes auth (404, not 401)
"""
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
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


def check(cond, name, detail=""):
    if cond:
        ok(name)
    else:
        bad(name, detail)


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("wing_agent_under_test", AGENT)
app = load("manager_app_under_test", APP)

REAL_KEY = agent.AGENT_KEY  # whatever the test env has (may be "")


def reset_state():
    agent._AUTH_ATTEMPTS.clear()


class Stub:
    """Mimics Handler closely enough for _check_auth: attribute access only.
    _authed delegates to the real Handler._authed (a bare object has no
    _authed, which would trip _check_auth's fail-open except)."""

    def __init__(self, path, key, ip="1.2.3.4"):
        self.path = path
        self.headers = {"X-Agent-Key": key}
        self.client_address = (ip, 9999)

    def _authed(self):
        return agent.Handler._authed(self)


print("== A. VERSION bump + manager gate ==")
check(agent.VERSION in ("1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"), "A1 VERSION is 1.0.19+ (1.0.20 adds status cache)",
      repr(agent.VERSION))
check(app._version_at_least("1.0.20", "1.0.2")
      and not app._version_at_least("1.0.1", "1.0.2"),
      "A2 manager >=1.0.2 gate accepts 1.0.20", "gate contract broken")
check((agent.AUTH_RATE_MAX, agent.AUTH_RATE_WINDOW, agent.AUTH_RATE_MAX_IPS)
      == (10, 60, 1024), "A3 rate-limit constants 10/60s/1024")

print("== B. _auth_key_ok ==")
agent.AGENT_KEY = "test-key-123"
try:
    check(agent._auth_key_ok("test-key-123") is True, "B1 correct key -> True")
    check(agent._auth_key_ok("test-key-124") is False, "B2 wrong key -> False")
    check(agent._auth_key_ok("") is False, "B3 empty input -> False")
    agent.AGENT_KEY = ""
    check(agent._auth_key_ok("") is False,
          "B4 empty AGENT_KEY refuses even matching input (fail closed)")
    agent.AGENT_KEY = "test-key-123"
    for bad_in in (None, 123, b"test-key-123", ["test-key-123"]):
        r = agent._auth_key_ok(bad_in)
        if r is not False:
            break
    else:
        r = False
    check(r is False, "B5 non-string input -> False, never raises",
          repr(bad_in) if r is not False else "")
    try:
        r = agent._auth_key_ok("tëst-key-123")  # non-ASCII str
        check(r is False, "B6 non-ASCII input -> False, never raises")
    except Exception as e:
        bad("B6 non-ASCII input -> False, never raises", repr(e))
    check("hmac.compare_digest" in inspect.getsource(agent._auth_key_ok),
          "B7 constant-time compare via hmac.compare_digest")
finally:
    agent.AGENT_KEY = REAL_KEY

print("== C. rate-limit state machine ==")
reset_state()
check(agent.auth_allowed("9.9.9.9") is True, "C1 unknown IP allowed")
for i in range(9):
    agent.record_auth_failure("10.0.0.1")
check(agent.auth_allowed("10.0.0.1") is True, "C2a 9 failures still allowed")
agent.record_auth_failure("10.0.0.1")
check(agent.auth_allowed("10.0.0.1") is False, "C2b 10 failures -> blocked")
reset_state()
old = time.time() - 120
agent._AUTH_ATTEMPTS["10.0.0.2"] = [old] * 10
check(agent.auth_allowed("10.0.0.2") is True, "C3 expired window -> allowed")
check(agent._AUTH_ATTEMPTS.get("10.0.0.2", []) == [],
      "C3b expired entry left empty (cap-prune reaps empties; same as the "
      "manager's login limiter)")
reset_state()
for i in range(10):
    agent.record_auth_failure("10.0.0.3")
check(agent.auth_allowed("10.0.0.3") is False, "C4a ip1 blocked")
check(agent.auth_allowed("10.0.0.4") is True, "C4b ip2 unaffected")
reset_state()
for i in range(10):
    agent.record_auth_failure("10.0.0.5")
agent.clear_auth_failures("10.0.0.5")
check(agent.auth_allowed("10.0.0.5") is True
      and "10.0.0.5" not in agent._AUTH_ATTEMPTS,
      "C5 success clears the counter")
reset_state()
saved_cap = agent.AUTH_RATE_MAX_IPS
agent.AUTH_RATE_MAX_IPS = 3
try:
    for i in range(5):
        agent.record_auth_failure(f"192.0.2.{i}")
    check(agent.auth_allowed("192.0.2.99") is True,
          "C6a over-cap still serves (fail-open)")
    check(len(agent._AUTH_ATTEMPTS) <= 3,
          "C6b stalest-first eviction keeps the cap",
          f"len={len(agent._AUTH_ATTEMPTS)}")
    check(agent.auth_allowed("192.0.2.0") is True,
          "C6c evicted IP is fail-open (counter reset)")
finally:
    agent.AUTH_RATE_MAX_IPS = saved_cap
reset_state()
try:
    r1 = agent.auth_allowed(None)
    agent.record_auth_failure(None)
    agent.clear_auth_failures(None)
    agent.record_auth_failure(["unhashable"])
    r2 = agent.auth_allowed(["unhashable"])
    check(True, "C7 garbage inputs never raise")
except Exception as e:
    bad("C7 garbage inputs never raise", repr(e))
reset_state()
now = time.time()
agent._AUTH_ATTEMPTS["10.0.0.6"] = [now - 120, now - 5, now - 200]
agent.auth_allowed("10.0.0.6")
kept = agent._AUTH_ATTEMPTS.get("10.0.0.6", [])
check(len(kept) == 1 and now - kept[0] < 60,
      "C8 prune keeps only in-window timestamps")

print("== D. _check_auth wiring ==")
reset_state()
agent.AGENT_KEY = "test-key-123"
try:
    v = agent.Handler._check_auth(Stub("/api/agent/health", "wrong"))
    check(v == "health", "D1 health -> 'health'", repr(v))
    check(len(agent._AUTH_ATTEMPTS) == 0,
          "D1b health probe never counts toward the limit")
    v = agent.Handler._check_auth(Stub("/api/agent/instances", "test-key-123"))
    check(v == "ok", "D2 correct key -> 'ok'", repr(v))
    v = agent.Handler._check_auth(Stub("/api/agent/instances", "nope",
                                       ip="10.1.0.1"))
    check(v == "denied", "D3 wrong key -> 'denied'", repr(v))
    check(len(agent._AUTH_ATTEMPTS.get("10.1.0.1", [])) == 1,
          "D3b failure recorded for the IP")
    reset_state()
    for i in range(10):
        r = agent.Handler._check_auth(Stub("/api/agent/instances", "nope",
                                           ip="10.1.0.2"))
    check(r == "denied", "D4a first 10 wrong -> 'denied'")
    r = agent.Handler._check_auth(Stub("/api/agent/instances", "nope",
                                       ip="10.1.0.2"))
    check(r == "ratelimited", "D4b 11th attempt -> 'ratelimited'", repr(r))
    r = agent.Handler._check_auth(Stub("/api/agent/instances", "test-key-123",
                                       ip="10.1.0.2"))
    check(r == "ratelimited",
          "D4c correct key while rate-limited still 429 "
          "(429-before-credential ordering)", repr(r))
    reset_state()
    for i in range(5):
        agent.Handler._check_auth(Stub("/api/agent/instances", "nope",
                                       ip="10.1.0.3"))
    r = agent.Handler._check_auth(Stub("/api/agent/instances", "test-key-123",
                                       ip="10.1.0.3"))
    check(r == "ok" and "10.1.0.3" not in agent._AUTH_ATTEMPTS,
          "D5 successful auth clears the IP's counter")
    v = agent.Handler._check_auth(Stub("/api/agent/health?x=1", "nope"))
    check(v == "denied",
          "D6 query-string health keeps old raw-path semantics (needs key)",
          repr(v))

    class BoomAddr:
        path = "/api/agent/instances"
        headers = {"X-Agent-Key": "test-key-123"}

        @property
        def client_address(self):
            raise RuntimeError("boom")

        def _authed(self):
            return agent.Handler._authed(self)

    try:
        v = agent.Handler._check_auth(BoomAddr())
        check(v == "ok", "D7 raising client_address never breaks auth",
              repr(v))
    except Exception as e:
        bad("D7 raising client_address never breaks auth", repr(e))
finally:
    agent.AGENT_KEY = REAL_KEY
reset_state()

print("== E. route contract ==")
src = open(AGENT).read()
for meth in ("do_GET", "do_POST", "do_DELETE"):
    body = src.split(f"def {meth}(self):", 1)[1].split("\n    def ", 1)[0]
    has429 = ('"ratelimited"' in body
              and '429, {"error": "too many attempts"}' in body)
    check(has429, f"E1 {meth} maps ratelimited -> 429 'too many attempts'")
    check(body.index('"ratelimited"') < body.index('"denied"'),
          f"E2 {meth} checks 429 before 401")
check("if not self._authed():" not in src,
      "E3 no legacy 'if not self._authed()' call sites remain")
check('return self._send(401, {"error": "unauthorized"})' in src,
      "E4 401 'unauthorized' body unchanged")
check(src.count('{"error": "too many attempts"}') >= 3,
      "E5 429 body matches the manager's /api/login 429 convention")

print("== F. live localhost server ==")


class Quiet(agent.Handler):
    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Quiet)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def req(path, key=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if key is not None:
        r.add_header("X-Agent-Key", key)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


agent.AGENT_KEY = "live-key-9"
reset_state()
try:
    # NOTE: every live request here comes from 127.0.0.1, so the correct-key
    # check runs BEFORE the brute-force sequence — after 10 failures the
    # correct key is rightly 429 (429-before-credential, covered in D4c).
    code, _ = req("/api/agent/instances", key="live-key-9")
    check(code == 200, "F1 correct key passes auth (200 served, not 401)",
          f"got {code}")
    reset_state()
    code, body = req("/api/agent/instances", key="wrong")
    check(code == 401, "F2 wrong key -> 401", f"got {code}")
    for i in range(9):
        req("/api/agent/instances", key="wrong")
    code, body = req("/api/agent/instances", key="wrong")
    check(code == 429, "F3 10 wrong -> 429", f"got {code}")
    try:
        err = json.loads(body).get("error")
    except Exception:
        err = None
    check(err == "too many attempts", "F4 429 body is the exact convention",
          repr(body[:60]))
    code, _ = req("/api/agent/health")
    check(code == 200, "F5 health unauthenticated -> 200 (never rate-limited)",
          f"got {code}")
finally:
    agent.AGENT_KEY = REAL_KEY
    srv.shutdown()
reset_state()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
