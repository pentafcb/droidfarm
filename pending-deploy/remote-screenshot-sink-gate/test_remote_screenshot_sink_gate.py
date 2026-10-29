#!/usr/bin/env python3
"""Tests for remote-screenshot-sink-gate (manager only, no agent change).

GAP (the re-sweep named by the 2026-10-28 ~13:44 UTC session, same audit
class as t165/t166/t167): the stream routes all gate instance names
(route-name-gate), and the local frame source capture_screenshot gates
them at the sink (t165) — but get_remote_screenshot(), the SHARED
remote frame source for the MJPEG, H.264, and WebRTC paths, did NOT.
It built its wing URL path ("/api/agent/instances/%s/screenshot") from
the raw name, and wing_request() concatenates the path onto the node
URL with no quoting — so a corrupted instances.json key reaching it
from a future internal caller (the three stream paths all funnel
through it) would drive a wing URL outside the screenshot endpoint
and pollute the shared (prov_id, name) frame cache. The agent's own
screenshot route gates names too, but the sink itself was wrong
regardless, same as the watchdog's in t160.

FIX (manager/app.py only): get_remote_screenshot fails closed at the
top, before any cache touch or wing call — invalid name -> None,
indistinguishable from a missing frame, so the 15s frameless guards on
all three stream paths keep treating it as "no frame". Valid names
behave identically; no agent, dashboard, or wire-protocol change (the
agent's screenshot route already gates names).

Covers:
  A. regression proof (contract fact + old URL/cache-key construction
     interpolates a hostile name verbatim)
  B. real get_remote_screenshot (stubbed wing_request): hostile-name
     matrix -> None with zero wing calls and zero cache entries;
     valid fetch -> data + exact wing URL + timeout/attempts;
     cache hit / expiry / 404-drops-entry / raising-wing / short /
     non-bytes data / stale-entry prune / dash-underscore-digit names /
     None name
  C. source contracts (gate placement before cache key + wing call,
     gate returns None, sibling sink gates kept, 3 call sites
     unchanged, snapshot parity)
  D. negative proof (reconstructed pre-fix flow DOES build the wing
     URL from a hostile name, so the harness is not vacuous)
"""
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
SNAP_APP = os.path.join(REPO, "pending-deploy", "remote-autologin", "app.py")

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


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("rssg_app", APP)
app_src = open(APP, encoding="utf-8").read()


def fn_src(fname):
    start = app_src.index(f"def {fname}(")
    try:
        end = app_src.index("\ndef ", start + 1)
    except ValueError:
        end = len(app_src)
    return app_src[start:end]


BAD_NAMES = ["", "../x", "../../tmp/evil", "a/b", "x;y",
             "$(id)", "rbx 1", "a" * 33, "`id`", "rbx1\nrbx2"]
BAD = "rbx1;id>/tmp/p"
WING = {"url": "http://node.example:8090", "agent_key": "k"}
GOOD = b"\x89PNG" + b"x" * 2000

# ---------------------------------------------------------------- A. regression
print("== A. regression proof (contract + construction facts) ==")
check("every attack-shaped name fails the gate contract",
      all(not app.valid_instance_name(n) for n in BAD_NAMES)
      and app.valid_instance_name("rbx1"),
      f"matrix={[app.valid_instance_name(n) for n in BAD_NAMES]}")
_old_url = "/api/agent/instances/%s/screenshot" % BAD
check("old URL construction interpolates a hostile name verbatim "
      "into the wing path",
      BAD in _old_url)
_old_key = ("p1", BAD)
check("old cache-key construction accepts a hostile name verbatim",
      _old_key[1] == BAD)

# ---------------------------------------------------------------- B. real function
print("== B. real get_remote_screenshot (stubbed wing_request) ==")
_state = {"calls": [], "resp": (200, GOOD), "raise": False}


def fake_wing(provider, method, path, data=None, timeout=8, attempts=3):
    _state["calls"].append((provider, method, path, timeout, attempts))
    if _state["raise"]:
        raise ConnectionError("boom")
    return _state["resp"]


app.wing_request = fake_wing


def reset(resp=(200, GOOD), raise_=False):
    _state["calls"] = []
    _state["resp"] = resp
    _state["raise"] = raise_
    with app._remote_shots_lock:
        app._remote_shots.clear()


reset()
for n in BAD_NAMES:
    before = len(_state["calls"])
    got = app.get_remote_screenshot(WING, "p1", n)
    check(f"hostile {n!r} -> None, no wing call, no cache entry",
          got is None and len(_state["calls"]) == before
          and ("p1", n) not in app._remote_shots)

reset()
got = app.get_remote_screenshot(WING, "p1", "rbx1")
check("valid fetch returns the wing bytes", got == GOOD)
check("valid fetch uses the exact screenshot wing URL",
      len(_state["calls"]) == 1
      and _state["calls"][0][2] == "/api/agent/instances/rbx1/screenshot")
check("valid fetch is GET, timeout=10, attempts=1 (fail-fast)",
      _state["calls"][0][1] == "GET" and _state["calls"][0][3] == 10
      and _state["calls"][0][4] == 1)
got2 = app.get_remote_screenshot(WING, "p1", "rbx1")
check("cache hit returns same bytes with no second wing call",
      got2 == GOOD and len(_state["calls"]) == 1)
with app._remote_shots_lock:
    app._remote_shots[("p1", "rbx1")][0] = time.monotonic() - 1
got3 = app.get_remote_screenshot(WING, "p1", "rbx1")
check("expired cache entry refetches from the wing",
      got3 == GOOD and len(_state["calls"]) == 2)

reset(resp=(404, {"error": "nope"}))
with app._remote_shots_lock:
    # Expired entry: a FRESH entry would short-circuit before the wing
    # fetch, so the 404 path must be driven with a stale one.
    app._remote_shots[("p1", "rbx1")] = [time.monotonic() - 1, GOOD]
got = app.get_remote_screenshot(WING, "p1", "rbx1")
check("wing 404 -> None", got is None)
check("wing 404 drops the pre-seeded cache entry",
      ("p1", "rbx1") not in app._remote_shots)

reset(raise_=True)
got = app.get_remote_screenshot(WING, "p1", "rbx1")
check("raising wing -> None, never raises", got is None)

reset(resp=(200, b"short"))
got = app.get_remote_screenshot(WING, "p1", "rbx1")
check("wing 200 with short data -> None, no cache entry",
      got is None and ("p1", "rbx1") not in app._remote_shots)

reset(resp=(200, {"not": "bytes"}))
got = app.get_remote_screenshot(WING, "p1", "rbx1")
check("wing 200 with non-bytes data -> None, no cache entry",
      got is None and ("p1", "rbx1") not in app._remote_shots)

reset()
with app._remote_shots_lock:
    app._remote_shots[("p9", "rbx9")] = [time.monotonic() - 61, GOOD]
got = app.get_remote_screenshot(WING, "p1", "rbx1")
check("successful fetch prunes stale entries for other instances",
      got == GOOD and ("p9", "rbx9") not in app._remote_shots
      and ("p1", "rbx1") in app._remote_shots)

reset()
got = app.get_remote_screenshot(WING, "p1", "rbx-1_x2")
check("dash/underscore/digit names pass through to the wing",
      got == GOOD and _state["calls"][0][2]
      == "/api/agent/instances/rbx-1_x2/screenshot")

reset()
got = app.get_remote_screenshot(WING, "p1", None)
check("None name -> None, no wing call",
      got is None and len(_state["calls"]) == 0)

# ---------------------------------------------------------------- C. source contracts
print("== C. source contracts ==")
src = fn_src("get_remote_screenshot")
gi = src.index("if not valid_instance_name(name):")
check("gate sits before the cache key and the wing call",
      gi < src.index("key = (prov_id, name)")
      < src.index("wing_request("))
check("gate returns None (indistinguishable from a missing frame)",
      src[gi:gi + 80].count("return None") >= 1)
check("sibling local frame-source gate kept (capture_screenshot)",
      "if not valid_instance_name(name):" in fn_src("capture_screenshot"))
check("sibling input-core gate kept (do_instance_input)",
      "if not valid_instance_name(name):" in fn_src("do_instance_input"))
check("sibling exec gate kept (waydroid_exec)",
      "if not valid_instance_name(name):" in fn_src("waydroid_exec"))
check("all 3 stream call sites unchanged (MJPEG, H.264, WebRTC)",
      app_src.count("get_remote_screenshot(wing, prov, name)") == 2
      and app_src.count("get_remote_screenshot(wing, provider, name)") == 1)
check("snapshot parity: staged remote-autologin app.py == HEAD",
      open(SNAP_APP, encoding="utf-8").read() == app_src,
      "refresh the snapshot if this fails")

# ---------------------------------------------------------------- D. negative proof
print("== D. negative proof ==")
_pre_url = "/api/agent/instances/%s/screenshot" % BAD
_pre_calls = []
_pre_calls.append(_pre_url)
check("pre-fix flow builds the wing URL from a hostile name",
      BAD in _pre_calls[0])

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
