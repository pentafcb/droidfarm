"""wing-ratelimit-tunnel: agent 1.0.28 proxy-aware auth rate-limit keys.

The wing agent's agent-key rate limiter (1.0.19: 10 failed / 60s / IP -> 429)
keyed on the TCP peer (client_address[0]). The manager's
login-ratelimit-tunnel (2026-10-20) fixed the same flaw on /api/login and
/api/providers/register: behind a reverse proxy / tunnel every user shares
the loopback peer, so one user's 10 bad attempts 429'd everyone -- including
the manager's own keyed requests (screenshot, status, self-update) to the
node. New _agent_client_ip(headers, peer) mirrors the manager's
_client_ip_from_headers: loopback peer -> CF-Connecting-IP first, then the
leftmost X-Forwarded-For entry (must parse as an IP literal via stdlib
ipaddress, else the peer is kept); non-loopback peers NEVER trust headers
(spoof-proof). Never raises; degrades to the old peer-keyed behavior.
_check_auth derives the limiter key through it before any limiter call.

Hermetic: fake headers/handlers + one live localhost HTTP server.
PASS=38 FAIL=0 expected.
"""
import importlib.util
import inspect
import json
import os
import sys
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


def check(cond, name, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("wing_agent_under_test_28", AGENT)
app = load("manager_app_under_test_28", APP)
REAL_KEY = agent.AGENT_KEY  # whatever the test env has (may be "")


def reset_state():
    agent._AUTH_ATTEMPTS.clear()


class H:
    """Case-insensitive-ish fake headers with a .get."""

    def __init__(self, d=None):
        self.d = dict(d or {})

    def get(self, name, default=None):
        return self.d.get(name, default)


class NoGet:
    """Headers object without a callable .get -> helper must degrade."""

    def __init__(self):
        self.get = None


class Stub:
    """Mimics Handler closely enough for _check_auth: attribute access
    only. _authed delegates to the real Handler._authed (a bare object has
    no _authed, which would trip _check_auth's fail-open except)."""

    def __init__(self, path, key, peer="127.0.0.1", headers=None):
        self.path = path
        self.headers = headers if headers is not None else H({"X-Agent-Key": key})
        if isinstance(self.headers, H) and "X-Agent-Key" not in self.headers.d:
            self.headers.d["X-Agent-Key"] = key
        self.client_address = (peer, 9999)

    def _authed(self):
        return agent.Handler._authed(self)

    def _check_auth(self):
        return agent.Handler._check_auth(self)


print("== A. VERSION + wiring ==")
check(agent.VERSION in ("1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"), "A1 VERSION is 1.0.28+ (1.0.29 adds missing-script guard)",
      repr(agent.VERSION))
check(agent.parse_agent_version("1.0.28") > agent.parse_agent_version("1.0.27"),
      "A2 parse_agent_version ordering: 1.0.28 > 1.0.27")
check(app._version_at_least("1.0.28", "1.0.2")
      and not app._version_at_least("1.0.1", "1.0.2"),
      "A3 manager >=1.0.2 gate accepts 1.0.28")
check(hasattr(agent, "_agent_client_ip")
      and "ipaddress" in inspect.getsource(agent._agent_client_ip),
      "A4 _agent_client_ip exists and uses stdlib ipaddress")
check("_agent_client_ip(" in inspect.getsource(agent.Handler._check_auth),
      "A5 _check_auth derives the limiter key via _agent_client_ip")
check("import ipaddress" in inspect.getsource(agent),
      "A6 agent imports stdlib ipaddress")

print("== B. _agent_client_ip units ==")
f = agent._agent_client_ip
check(f(H({"CF-Connecting-IP": "203.0.113.7"}), "127.0.0.1") == "203.0.113.7",
      "B1 loopback + CF-Connecting-IP -> CF IP")
check(f(H({"CF-Connecting-IP": "203.0.113.7",
           "X-Forwarded-For": "198.51.100.9"}), "127.0.0.1") == "203.0.113.7",
      "B2 CF-Connecting-IP wins over X-Forwarded-For")
check(f(H({"X-Forwarded-For": " 198.51.100.9 , 10.0.0.5"}), "127.0.0.1")
      == "198.51.100.9",
      "B3 XFF leftmost entry, trimmed")
check(f(H({"CF-Connecting-IP": "not-an-ip",
           "X-Forwarded-For": "198.51.100.9"}), "127.0.0.1") == "198.51.100.9",
      "B4 garbage CF falls through to XFF")
check(f(H({"CF-Connecting-IP": "nope", "X-Forwarded-For": "also-nope"}),
        "127.0.0.1") == "127.0.0.1",
      "B5 garbage headers -> peer kept")
check(f(H({}), "127.0.0.1") == "127.0.0.1", "B6 no headers -> peer kept")
check(f(NoGet(), "127.0.0.1") == "127.0.0.1",
      "B7 headers without callable .get -> peer kept")
check(f(H({"CF-Connecting-IP": "203.0.113.7"}), "203.0.113.1") == "203.0.113.1",
      "B8 non-loopback public peer ignores headers (spoof-proof)")
check(f(H({"CF-Connecting-IP": "203.0.113.7"}), "10.1.2.3") == "10.1.2.3",
      "B9 non-loopback private peer ignores headers")
check(f(H({"CF-Connecting-IP": "203.0.113.7"}), "::1") == "203.0.113.7",
      "B10 ::1 is loopback -> CF IP used")
check(f(H({"CF-Connecting-IP": "2001:0DB8::1"}), "127.0.0.1") == "2001:db8::1",
      "B11 IPv6 candidate normalized via ipaddress")
check(f(H({"CF-Connecting-IP": "203.0.113.7"}), "2001:db8::5")
      == "2001:db8::5",
      "B12 non-loopback IPv6 peer ignores headers")
check(f(H({"CF-Connecting-IP": "203.0.113.7"}), "?") == "?",
      "B13 garbage peer -> returned as-is (fail closed to old behavior)")
check(f(None, "127.0.0.1") == "127.0.0.1", "B14 headers None -> peer kept")
check(f(H({"CF-Connecting-IP": "example.com"}), "127.0.0.1") == "127.0.0.1",
      "B15 hostname is not an IP literal -> peer kept")
check(f(H({"CF-Connecting-IP": "203.0.113.7:8443"}), "127.0.0.1")
      == "127.0.0.1",
      "B16 host:port is not an IP literal -> peer kept")
for hostile in (None, 123, b"203.0.113.7", ["203.0.113.7"], object()):
    try:
        f(H({"CF-Connecting-IP": hostile}), "127.0.0.1")
        f(None, None)
        r = True
    except Exception as e:
        r = e
        break
else:
    r = True
check(r is True, "B17 never raises on hostile headers/peers")

print("== C. _check_auth bucket isolation ==")
agent.AGENT_KEY = "tunnel-key-1"
try:
    reset_state()
    hdrs_a = lambda k: H({"X-Agent-Key": k, "CF-Connecting-IP": "203.0.113.7"})
    hdrs_b = lambda k: H({"X-Agent-Key": k, "CF-Connecting-IP": "203.0.113.8"})
    p = "/api/agent/instances"
    for _ in range(10):
        Stub(p, "wrong", headers=hdrs_a("wrong"))._check_auth()
    check(Stub(p, "wrong", headers=hdrs_a("wrong"))._check_auth()
          == "ratelimited",
          "C1 tunneled user A: 10 bad -> ratelimited")
    check(Stub(p, "tunnel-key-1", headers=hdrs_b("tunnel-key-1"))._check_auth()
          == "ok",
          "C2 tunneled user B (same peer, other CF IP): correct key -> ok")
    check(Stub(p, "wrong", headers=hdrs_b("wrong"))._check_auth() == "denied",
          "C3 B's own failures counted separately (denied, not ratelimited)")
    check(Stub(p, "tunnel-key-1")._check_auth() == "ok",
          "C4 manager-direct 127.0.0.1 (no headers): own bucket, not 429'd")
    check(Stub(p, "tunnel-key-1", headers=hdrs_a("tunnel-key-1"))._check_auth()
          == "ratelimited",
          "C5 A with correct key while limited -> still ratelimited "
          "(429-before-credential)")
    # Spoof attempt: public peer sends a forged header; the bucket must stay
    # keyed on the peer.
    reset_state()
    spoof = lambda k: H({"X-Agent-Key": k, "CF-Connecting-IP": "1.1.1.1"})
    for _ in range(10):
        Stub(p, "wrong", peer="198.51.100.44", headers=spoof("wrong"))._check_auth()
    check(Stub(p, "tunnel-key-1", peer="198.51.100.44",
               headers=spoof("tunnel-key-1"))._check_auth() == "ratelimited",
          "C6 public peer: failures keyed on peer, not the forged header")
    check(Stub(p, "tunnel-key-1", peer="198.51.100.45",
               headers=spoof("tunnel-key-1"))._check_auth() == "ok",
          "C7 same forged header from another peer -> separate bucket (ok)")
    check(Stub("/api/agent/health", "wrong",
               headers=hdrs_a("wrong"))._check_auth() == "health",
          "C8 /api/agent/health stays unauthenticated + uncounted")
    # Success clears only the caller's own (header-derived) bucket.
    reset_state()
    for _ in range(10):
        Stub(p, "wrong", headers=hdrs_a("wrong"))._check_auth()
    check(Stub(p, "tunnel-key-1", headers=hdrs_b("tunnel-key-1"))._check_auth()
          == "ok"
          and Stub(p, "wrong", headers=hdrs_a("wrong"))._check_auth()
          == "ratelimited",
          "C9 B's success does not clear A's bucket")
finally:
    agent.AGENT_KEY = REAL_KEY
    reset_state()

print("== D. live localhost server ==")


class Quiet(agent.Handler):
    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Quiet)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def req(path, key=None, headers=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if key is not None:
        r.add_header("X-Agent-Key", key)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


agent.AGENT_KEY = "live-key-28"
reset_state()
try:
    CF1 = {"CF-Connecting-IP": "203.0.113.71"}
    CF2 = {"CF-Connecting-IP": "203.0.113.72"}
    for _ in range(10):
        req("/api/agent/instances", key="wrong", headers=CF1)
    code, body = req("/api/agent/instances", key="wrong", headers=CF1)
    check(code == 429, "D1 tunneled user: 10 wrong -> 429", f"got {code}")
    try:
        err = json.loads(body).get("error")
    except Exception:
        err = None
    check(err == "too many attempts", "D2 429 body is the exact convention",
          repr(body[:60]))
    code, _ = req("/api/agent/instances", key="live-key-28", headers=CF1)
    check(code == 429, "D3 tunneled user with correct key -> still 429",
          f"got {code}")
    code, _ = req("/api/agent/instances", key="live-key-28", headers=CF2)
    check(code == 200, "D4 other tunneled user: separate bucket, 200",
          f"got {code}")
    code, _ = req("/api/agent/instances", key="wrong")
    check(code == 401, "D5 no-header request on 127.0.0.1: peer bucket untouched"
          " -> 401 not 429", f"got {code}")
    code, _ = req("/api/agent/health")
    check(code == 200, "D6 health -> 200 unauthenticated", f"got {code}")
finally:
    agent.AGENT_KEY = REAL_KEY
    srv.shutdown()
    reset_state()

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
