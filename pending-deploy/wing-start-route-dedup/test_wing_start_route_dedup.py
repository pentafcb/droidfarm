#!/usr/bin/env python3
"""Tests for wing start-route dedup (agent 1.0.34).

REAL GAP (maintenance trap, not a live bug): wing/agent.py do_POST had TWO
routes matching POST /api/agent/instances/<n>/start -- the dedicated /start
route (with _sw_prepare_pools_for_start(), added by binder-safe-start) and
an older (start|stop) route whose `start` alternative was unreachable dead
code (only its `stop` branch was live). The next person to touch the start
path could easily edit the dead branch thinking it runs.

Fix: the (start|stop) route is narrowed to /stop only; the live /start
route (with pool prepare) is untouched. VERSION 1.0.33 -> 1.0.34.

Run: python3 test_wing_start_route_dedup.py
Hermetic: source inspection only, no docker, no network.

Covers:
  A. VERSION is 1.0.34
  B. no (start|stop) alternation remains in do_POST
  C. exactly one /start route and one /stop route in do_POST
  D. the /stop route unconditionally stops (no action conditional)
  E. the live /start route still runs _sw_prepare_pools_for_start()
  F. start/stop/create routes still invalidate the status cache
"""
import importlib.util
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")

PASS = FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok: {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {name} {detail}")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("wa_srd", AGENT)
post_src = inspect.getsource(agent.Handler.do_POST)

# ---- A. version ----
if agent.VERSION in ("1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"):
    ok("A VERSION is 1.0.34+ (exact 1.0.35 now owned by wing-install-guard)")
else:
    bad("A VERSION", repr(agent.VERSION))

# ---- B. dead alternation gone ----
if "(start|stop)" not in post_src:
    ok("B no (start|stop) alternation remains in do_POST")
else:
    bad("B (start|stop) still present", "")

# ---- C. exactly one /start and one /stop route ----
# (plain substring count: the route regexes are the only places these
# exact literals appear in do_POST; no regex escaping needed)
start_routes = post_src.count('([^/]+)/start"')
stop_routes = post_src.count('([^/]+)/stop"')
if start_routes == 1:
    ok("C exactly one /start route in do_POST")
else:
    bad("C /start route count", str(start_routes))
if stop_routes == 1:
    ok("C exactly one /stop route in do_POST")
else:
    bad("C /stop route count", str(stop_routes))

# ---- D. /stop unconditionally stops ----
stop_block = post_src.split('/stop"', 1)[1].split("m = re.fullmatch", 1)[0] \
    if '/stop"' in post_src else ""
if 'docker("stop", cname' in stop_block:
    ok("D /stop route calls docker stop unconditionally")
else:
    bad("D /stop docker call", stop_block[:120])
if "action" not in stop_block:
    ok("D no action conditional left in /stop route")
else:
    bad("D action variable still referenced", "")

# ---- E. live /start route keeps pool prepare ----
start_block = post_src.split('/start"', 1)[1].split("m = re.fullmatch", 1)[0] \
    if '/start"' in post_src else ""
if "_sw_prepare_pools_for_start()" in start_block:
    ok("E /start route still prepares pools before docker start")
else:
    bad("E pool prepare missing from /start route", "")
if 'docker("start", cname' in start_block:
    ok("E /start route still docker-starts")
else:
    bad("E /start docker call", "")

# ---- F. cache invalidation preserved ----
n = post_src.count("invalidate_agent_status_cache(name)")
if n == 4:
    ok("F start/stop/create/resize routes still invalidate (4 calls)")
else:
    bad("F invalidation count", str(n))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
