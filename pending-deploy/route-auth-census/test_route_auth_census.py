#!/usr/bin/env python3
"""Route-auth census (t157): every HTTP route on the manager and the wing
agent must prove its authentication, or be explicitly classified.

WHY: the manager dashboard gate (POST /api/login) sits on a public VPS and
the wing agent port is reachable from the network. Both surfaces grew
route-by-route, each branch carrying its own auth check (manager do_GET)
or a global gate (manager do_POST/do_DELETE, all agent methods). A new
route added without an auth check would ship silently open — no existing
suite enumerates the route set, so nothing would fail. This census fails
closed: it extracts every route branch from the Handler methods and
requires each one to be either (a) behind a proven auth check, or (b) in
an explicit allowlist with its own auth mechanism documented. An
unclassified route (added, renamed, or moved) fails the suite until it is
classified here AND wired into the preflight count.

Hermetic: pure source analysis, no network, no docker, no server boot.
Run: python3 test_route_auth_census.py  ->  PASS=N FAIL=0 on stdout.
"""

import os
import re
import sys

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL: {name}")


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
APP = os.path.join(ROOT, "manager", "app.py")
AGENT = os.path.join(ROOT, "wing", "agent.py")

app_src = open(APP, encoding="utf-8").read()
agent_src = open(AGENT, encoding="utf-8").read()

# Route-branch patterns inside a Handler method body. Only these shapes
# count as routes: an f-string URL passed to wing_request, a validation
# regex over a name, or a comment mentioning a path must never match.
ROUTE_RES = [
    re.compile(r'(?:path|parsed\.path)\s*==\s*"([^"]+)"'),
    re.compile(r'path\.startswith\("([^"]+)"\)'),
    re.compile(r're\.fullmatch\(r"([^"]+)",\s*(?:path|parsed\.path)\)'),
]


def method_src(src, name):
    """Body of `    def <name>(...):` inside the Handler class."""
    start = src.index(f"    def {name}(")
    nxt = src.find("\n    def ", start + 1)
    return src[start:] if nxt == -1 else src[start:nxt]


def fn_src(src, name):
    """Body of a module-level `def <name>(` function."""
    start = src.index(f"def {name}(")
    nxt = src.find("\ndef ", start + 1)
    return src[start:] if nxt == -1 else src[start:nxt]


def routes_of(body):
    """{route -> [branch line indices]} for one method body."""
    found = {}
    for i, line in enumerate(body.split("\n")):
        for rx in ROUTE_RES:
            for m in rx.finditer(line):
                found.setdefault(m.group(1), []).append(i)
    return found


def branch_lines(body):
    """Sorted unique line indices where any route branch starts."""
    lines = set()
    for hits in routes_of(body).values():
        lines.update(hits)
    return sorted(lines)


def segment(body, start_line, branches):
    """Source lines from a branch line up to (not incl.) the next branch."""
    lines = body.split("\n")
    nxt = [b for b in branches if b > start_line]
    end = min(nxt) if nxt else len(lines)
    return "\n".join(lines[start_line:end])


def gate_line(body, needle):
    """First line index containing needle, or None."""
    for i, line in enumerate(body.split("\n")):
        if needle in line:
            return i
    return None


# ---------------------------------------------------------------- A. shape
print("== A. handler shape ==")
for src, label in ((app_src, "manager"), (agent_src, "agent")):
    for verb in ("do_GET", "do_POST", "do_DELETE"):
        check(f"{label} has {verb}",
              f"    def {verb}(self):" in src)
    # A new verb handler is a new unaudited surface: fail until classified.
    for verb in ("do_PUT", "do_PATCH", "do_HEAD", "do_OPTIONS", "do_TRACE"):
        check(f"{label} has no {verb} handler (would be unaudited)",
              f"    def {verb}(self):" not in src)

# ------------------------------------------------- B. manager do_GET
print("== B. manager do_GET: per-branch auth ==")
get_body = method_src(app_src, "do_GET")
get_routes = routes_of(get_body)
get_branches = branch_lines(get_body)

EXPECTED_GET = {
    "/ws/screen/", "/ws/h264/", "/api/agent/update/download",
    "/", "/static/", "/api/host-stats", "/api/watchdog-events",
    "/api/backup-status", "/api/providers/static-token",
    "/api/providers/install-progress", "/api/providers", "/api/install/",
    r"/api/instances/batch/([0-9a-f]+)", "/api/instances",
    r"/api/instances/([^/]+)/roblox", "/api/roblox-accounts",
    r"/api/instances/([^/]+)/roblox-account",
    r"/api/instances/([^/]+)/roblox-login-result",
}
check("manager GET route set == expected (no missing, no extra)",
      set(get_routes) == EXPECTED_GET)

# Public page: the dashboard HTML itself is public by design (login is
# client-side against /api/login); it must serve only via _serve_static,
# whose traversal guard is covered by its own contract, not by auth.
for route in ("/", "/static/"):
    seg = segment(get_body, get_routes[route][0], get_branches)
    check(f"manager GET {route} serves via _serve_static only",
          "_serve_static" in seg and "_authed()" not in seg)

# Agent-key authenticated download (agents pull their own update).
seg = segment(get_body, get_routes["/api/agent/update/download"][0],
              get_branches)
check("manager GET /api/agent/update/download checks X-Agent-Key",
      "X-Agent-Key" in seg and "compare_digest" in seg)

# Join-token authenticated installer files.
seg = segment(get_body, get_routes["/api/install/"][0], get_branches)
check("manager GET /api/install/ checks X-Join-Token",
      "X-Join-Token" in seg)

# WebSocket streams delegate auth to their handlers (checked in E).
for route in ("/ws/screen/", "/ws/h264/"):
    seg = segment(get_body, get_routes[route][0], get_branches)
    check(f"manager GET {route} delegates to a _handle_ws handler",
          "_handle_ws" in seg)

# Every other GET branch must carry its own _authed() check.
for route in sorted(EXPECTED_GET):
    if route in ("/", "/static/", "/api/agent/update/download",
                 "/api/install/", "/ws/screen/", "/ws/h264/"):
        continue
    seg = segment(get_body, get_routes[route][0], get_branches)
    check(f"manager GET {route} checks _authed()",
          "_authed()" in seg)

# ------------------------------------------------ C. manager do_POST
print("== C. manager do_POST: global gate + public-route auth ==")
post_body = method_src(app_src, "do_POST")
post_routes = routes_of(post_body)
post_branches = branch_lines(post_body)

EXPECTED_POST = {
    "/api/login", "/api/providers/register",
    "/api/providers/install-progress", "/api/providers/heartbeat",
    "/api/providers/join-token", "/api/providers/static-token/regenerate",
    "/api/instances", "/api/instances/batch",
    r"/api/instances/batch/([0-9a-f]+)/resume",
    "/api/instances/fleet/plan", "/api/instances/fleet",
    "/api/roblox-accounts/bulk",
    r"/api/instances/([^/]+)/(start|stop|restart)",
    r"/api/instances/([^/]+)/resize", r"/api/instances/([^/]+)/input",
    r"/api/instances/([^/]+)/webrtc/offer",
    r"/api/instances/([^/]+)/launch-roblox",
    r"/api/instances/([^/]+)/roblox-account",
    r"/api/instances/([^/]+)/roblox-login",
}
check("manager POST route set == expected (no missing, no extra)",
      set(post_routes) == EXPECTED_POST)

gate = gate_line(post_body, "if not self._authed():")
check("manager POST has a global auth gate", gate is not None)

PUBLIC_POST = {
    "/api/login": ("login_allowed", "check_dashboard_password"),
    "/api/providers/register": ("join_token", "compare_digest"),
    "/api/providers/install-progress": ("join_token",),
    "/api/providers/heartbeat": ("agent_key", "compare_digest"),
}
for route, needles in PUBLIC_POST.items():
    line = post_routes[route][0]
    seg = segment(post_body, line, post_branches)
    check(f"manager POST {route} sits BEFORE the global gate",
          gate is not None and line < gate)
    check(f"manager POST {route} carries its own auth evidence",
          all(n in seg for n in needles))
pre_gate = {r for r, hits in post_routes.items()
            if gate is not None and hits[0] < gate}
check("manager POST pre-gate routes == exactly the public set",
      pre_gate == set(PUBLIC_POST))
for route in sorted(EXPECTED_POST):
    if route in PUBLIC_POST:
        continue
    check(f"manager POST {route} sits AFTER the global gate",
          gate is not None and post_routes[route][0] > gate)

# ------------------------------------------------ D. manager do_DELETE
print("== D. manager do_DELETE: gate-first ==")
del_body = method_src(app_src, "do_DELETE")
del_routes = routes_of(del_body)
del_branches = branch_lines(del_body)
EXPECTED_DELETE = {
    "/api/providers/install-progress", r"/api/providers/([^/]+)",
    r"/api/instances/batch/([0-9a-f]+)", r"/api/instances/([^/]+)",
}
check("manager DELETE route set == expected (no missing, no extra)",
      set(del_routes) == EXPECTED_DELETE)
dgate = gate_line(del_body, "if not self._authed():")
check("manager DELETE gate precedes every route branch",
      dgate is not None and all(hits[0] > dgate
                                for hits in del_routes.values()))

# ------------------------------------------- E. manager WS + auth helpers
print("== E. manager WS handlers + _authed integrity ==")
ws = method_src(app_src, "_handle_ws")
ws_h = method_src(app_src, "_handle_ws_h264")
check("MJPEG _handle_ws checks _authed_ws()", "_authed_ws()" in ws)
check("H.264 _handle_ws_h264 checks _authed_ws()", "_authed_ws()" in ws_h)
authed_ws = method_src(app_src, "_authed_ws")
check("_authed_ws delegates to _authed()", "_authed()" in authed_ws)
authed = method_src(app_src, "_authed")
check("manager _authed requires a Bearer token",
      'startswith("Bearer ")' in authed)
check("manager _authed enforces token expiry",
      "_tokens" in authed and "time.time()" in authed)

# ------------------------------------------------------ F. wing agent
print("== F. wing agent: gate-first in every verb ==")
for verb in ("do_GET", "do_POST", "do_DELETE"):
    body = method_src(agent_src, verb)
    rts = routes_of(body)
    brs = branch_lines(body)
    gate = gate_line(body, "self._check_auth()")
    check(f"agent {verb} calls _check_auth before any route",
          gate is not None and all(hits[0] > gate
                                   for hits in rts.values()))

EXPECTED_AGENT_GET = {
    "/api/agent/health", "/api/agent/stats", "/api/agent/headroom",
    "/api/agent/instances",
    r"/api/agent/instances/([^/]+)/screenshot",
    r"/api/agent/instances/([^/]+)/status",
    r"/api/agent/instances/([^/]+)/logs",
    "/api/agent/selfwatch-events",
}
agent_get = routes_of(method_src(agent_src, "do_GET"))
check("agent GET route set == expected (no missing, no extra)",
      set(agent_get) == EXPECTED_AGENT_GET)
# Health is public by design (self-check + manager probes) and returns no
# credentials or instance data; the exemption must live in _check_auth.
check_auth = method_src(agent_src, "_check_auth")
check("agent _check_auth exempts ONLY /api/agent/health",
      check_auth.count('if self.path == "/api/agent/health":') == 1
      and 'return "health"' in check_auth)
check("agent _authed exempts ONLY /api/agent/health",
      method_src(agent_src, "_authed").count("/api/agent/health") == 1)

EXPECTED_AGENT_POST = {
    "/api/agent/instances",
    r"/api/agent/instances/([^/]+)/start",
    r"/api/agent/instances/([^/]+)/input",
    r"/api/agent/instances/([^/]+)/launch-roblox",
    r"/api/agent/instances/([^/]+)/stop",
    r"/api/agent/instances/([^/]+)/resize",
}
agent_post = routes_of(method_src(agent_src, "do_POST"))
check("agent POST route set == expected (no missing, no extra)",
      set(agent_post) == EXPECTED_AGENT_POST)

EXPECTED_AGENT_DELETE = {r"/api/agent/instances/([^/]+)"}
agent_del = routes_of(method_src(agent_src, "do_DELETE"))
check("agent DELETE route set == expected (no missing, no extra)",
      set(agent_del) == EXPECTED_AGENT_DELETE)

auth_key = fn_src(agent_src, "_auth_key_ok")
check("agent _auth_key_ok refuses an empty/unset key",
      "if not AGENT_KEY" in auth_key)
check("agent _auth_key_ok uses constant-time compare",
      "compare_digest" in auth_key)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
