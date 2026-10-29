"""Unit tests for tunnel-aware rate-limit client keys (manager/app.py).

Run: python3 test_login_ratelimit_tunnel.py
Covers:
  _client_ip_from_headers(headers, peer): loopback peer + CF-Connecting-IP /
    X-Forwarded-For -> forwarded IP; CF-Connecting-IP wins; XFF leftmost
    entry; garbage/empty headers fall back to the peer (fail closed);
    non-loopback peers NEVER trust headers (spoof-proof); ::1 and the
    127/8 range count as loopback; IPv6 candidates normalized; never raises
    on hostile input.
  integration: two dashboard users behind the same Cloudflare tunnel get
    separate login-failure buckets (10 mistyped passwords by user A 429s A,
    not B); direct-IP users unaffected; clear_login_failures resets the
    right bucket; garbage headers degrade to the old peer-keyed behavior.
  route contract: /api/login and /api/providers/register both derive the
    limiter key via _client_ip_from_headers before any limiter call; the
    helper and `import ipaddress` exist; no bare `ip = self.client_address[0]`
    remains in either branch.

Hermetic: app.py loaded via importlib (LOGIN_ATTEMPTS reset between tests);
no network, no docker.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL: {name}")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("tunnel_app", os.path.join(REPO, "manager", "app.py"))
APP_SRC = open(os.path.join(REPO, "manager", "app.py")).read()
f = app._client_ip_from_headers


class H(dict):
    """Minimal stand-in for http.client.HTTPMessage (only .get is used)."""
    pass


def reset():
    app.LOGIN_ATTEMPTS.clear()


# ---------------- helper: tunneled (loopback peer) ----------------
check("loopback + CF-Connecting-IP -> forwarded",
      f(H({"CF-Connecting-IP": "203.0.113.7"}), "127.0.0.1") == "203.0.113.7")
check("CF-Connecting-IP preferred over XFF",
      f(H({"CF-Connecting-IP": "203.0.113.7",
           "X-Forwarded-For": "198.51.100.9"}), "127.0.0.1") == "203.0.113.7")
check("loopback + XFF single -> XFF",
      f(H({"X-Forwarded-For": "198.51.100.9"}), "127.0.0.1") == "198.51.100.9")
check("loopback + XFF list -> leftmost",
      f(H({"X-Forwarded-For": "198.51.100.9, 70.41.3.20, 10.0.0.1"}),
        "127.0.0.1") == "198.51.100.9")
check("loopback + XFF with spaces trimmed",
      f(H({"X-Forwarded-For": "  198.51.100.9  "}), "127.0.0.1") == "198.51.100.9")
check("loopback + garbage CF-CIP falls through to valid XFF",
      f(H({"CF-Connecting-IP": "not-an-ip",
           "X-Forwarded-For": "198.51.100.9"}), "127.0.0.1") == "198.51.100.9")
check("loopback + garbage both headers -> peer (fail closed)",
      f(H({"CF-Connecting-IP": "evil", "X-Forwarded-For": "also-evil"}),
        "127.0.0.1") == "127.0.0.1")
check("loopback + no headers -> peer",
      f(H({}), "127.0.0.1") == "127.0.0.1")
check("::1 peer + CF-Connecting-IP -> forwarded",
      f(H({"CF-Connecting-IP": "203.0.113.7"}), "::1") == "203.0.113.7")
check("127.0.0.2 peer (loopback range) + XFF -> forwarded",
      f(H({"X-Forwarded-For": "198.51.100.9"}), "127.0.0.2") == "198.51.100.9")
check("loopback + IPv6 XFF normalized",
      f(H({"X-Forwarded-For": "2001:DB8::1"}), "127.0.0.1") == "2001:db8::1")
check("loopback + empty CF-CIP falls through to XFF",
      f(H({"CF-Connecting-IP": "", "X-Forwarded-For": "198.51.100.9"}),
        "127.0.0.1") == "198.51.100.9")
check("loopback + candidate with port rejected -> peer",
      f(H({"X-Forwarded-For": "198.51.100.9:8080"}), "127.0.0.1") == "127.0.0.1")

# ---------------- helper: direct (non-loopback peer) ----------------
check("public peer + CF-Connecting-IP -> peer (spoof ignored)",
      f(H({"CF-Connecting-IP": "1.1.1.1"}), "203.0.113.9") == "203.0.113.9")
check("public peer + XFF -> peer (spoof ignored)",
      f(H({"X-Forwarded-For": "1.1.1.1"}), "203.0.113.9") == "203.0.113.9")
check("private peer (10/8) + headers -> peer",
      f(H({"CF-Connecting-IP": "1.1.1.1"}), "10.0.0.5") == "10.0.0.5")
check("IPv6 public peer + headers -> peer",
      f(H({"CF-Connecting-IP": "1.1.1.1"}), "2001:db8::99") == "2001:db8::99")
check("public peer, no headers -> peer",
      f(H({}), "203.0.113.9") == "203.0.113.9")

# ---------------- helper: never raises ----------------
check("never raises: headers None",
      f(None, "127.0.0.1") == "127.0.0.1")


class BadHeaders:
    def get(self, name):
        raise RuntimeError("boom")


check("never raises: headers.get raises",
      f(BadHeaders(), "127.0.0.1") == "127.0.0.1")
check("never raises: garbage peer",
      f(H({"CF-Connecting-IP": "203.0.113.7"}), "not-an-ip") == "not-an-ip")
check("never raises: peer None",
      f(H({"CF-Connecting-IP": "203.0.113.7"}), None) is None)
check("never raises: non-string header value",
      f(H({"CF-Connecting-IP": 12345}), "127.0.0.1") == "127.0.0.1")

# ---------------- integration: separate buckets behind one tunnel ----------------
reset()
peer = "127.0.0.1"
key_a = f(H({"CF-Connecting-IP": "203.0.113.7"}), peer)
key_b = f(H({"CF-Connecting-IP": "198.51.100.9"}), peer)
for _ in range(10):
    app.record_login_failure(key_a)
check("tunnel user A blocked after 10 failures",
      app.login_allowed(key_a) is False)
check("tunnel user B still allowed (separate bucket)",
      app.login_allowed(key_b) is True)
app.clear_login_failures(key_a)
check("clear resets the right bucket",
      app.login_allowed(key_a) is True and app.login_allowed(key_b) is True)

reset()
direct_key = f(H({}), "203.0.113.9")
for _ in range(10):
    app.record_login_failure(direct_key)
check("direct-IP user blocked independently",
      app.login_allowed(direct_key) is False
      and app.login_allowed(f(H({}), "203.0.113.10")) is True)

reset()
garbage_key = f(H({"CF-Connecting-IP": "evil"}), peer)
for _ in range(10):
    app.record_login_failure(garbage_key)
check("garbage headers degrade to peer bucket (old behavior)",
      garbage_key == peer and app.login_allowed(peer) is False)

# ---------------- route contract ----------------
start = APP_SRC.index('if path == "/api/login":')
nxt = APP_SRC.index('if path == "/api/providers/register":', start)
login_block = APP_SRC[start:nxt]
nxt2 = APP_SRC.index('if path == "/api/providers/install-progress"', nxt)
reg_block = APP_SRC[nxt:nxt2]

check("helper exists", "def _client_ip_from_headers(" in APP_SRC)
check("import ipaddress", "\nimport ipaddress\n" in APP_SRC)
check("route: /api/login derives key via helper before limiter",
      "_client_ip_from_headers(" in login_block
      and login_block.index("_client_ip_from_headers(")
      < login_block.index("login_allowed("))
check("route: no bare peer key left in /api/login",
      "ip = self.client_address[0]" not in login_block)
check("route: /api/providers/register derives key via helper",
      "_client_ip_from_headers(" in reg_block)
check("route: no bare peer key left in register branch",
      "ip = self.client_address[0]" not in reg_block)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
