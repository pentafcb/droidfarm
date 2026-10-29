#!/usr/bin/env python3
"""Tests for input-sink-gate (manager only, no agent change).

GAP (security hardening, found by auditing sink-gate coverage after
t165): the HTTP input route and the WebRTC offer route both gate
instance names (valid_instance_name), but do_instance_input() — the
shared input core, also reached from the WebRTC data channel
(webrtc_input_handler, whose name comes from the _webrtc_pcs registry
rather than a freshly-gated route capture) — did NOT gate at the sink.
Its local branch only failed closed indirectly: container_status() ->
docker_inspect()'s wd- gate made a hostile name read as "not
running" before the FIFO path was opened, so the FIFO open was safe
only as long as that indirect gate stayed in place. Its remote branch
had NO gate at all: the name went straight into the wing_request URL
path (an authenticated dashboard user reaching a future internal
caller, or a corrupted instances.json key via the WebRTC registry,
could drive a wing request at an arbitrary agent path segment).

FIX (manager/app.py only): do_instance_input() fails closed at the
top, before any wing call, FIFO open, or waydroid_exec fallback —
invalid name -> (False, {"error": "instance not found"}),
indistinguishable from an unknown instance, exactly like the routes.
Valid names behave identically. No agent, dashboard, or wire-protocol
change.

Covers:
  A. regression proof (contract fact + old URL/FIFO constructions
     interpolate a hostile name verbatim + the indirect-gate fact)
  B. real do_instance_input (stubbed I/O): hostile names -> the
     fail-closed tuple with zero wing calls (remote), zero FIFO
     opens and zero waydroid_exec calls (local); valid remote tap
     still proxies with the exact URL; valid local tap still writes
     the exact FIFO path/command; stopped -> "instance not
     running"; FIFO failure -> waydroid_exec fallback (ok/fail);
     input validation unchanged (unknown type, bad coords)
  C. source contracts (gate placement, sibling sink gates kept,
     route gates kept, snapshot parity)
  D. negative proof (reconstructed pre-fix remote flow DOES reach
     wing_request with the hostile URL, so the harness is not vacuous)
"""
import importlib.util
import os
import sys

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


app = load("isg_app", APP)
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

# ---------------------------------------------------------------- A. regression
print("== A. regression proof (contract + construction facts) ==")
check("every attack-shaped name fails the gate contract",
      all(not app.valid_instance_name(n) for n in BAD_NAMES)
      and app.valid_instance_name("rbx1"),
      f"matrix={[app.valid_instance_name(n) for n in BAD_NAMES]}")
_old_url = "/api/agent/instances/rbx1;id>/tmp/p/input"
check("old remote construction interpolates a hostile name verbatim "
      "into the wing URL path",
      "rbx1;id>/tmp/p" in _old_url)
_old_fifo = ("/var/lib/docker/volumes/wd-../../tmp/evil-data/_data/"
             "data/media/0/jerusalem/input.fifo")
check("old local construction interpolates a hostile name verbatim "
      "into the FIFO path",
      "wd-../../tmp/evil-data" in _old_fifo)
check("pre-fix local branch relied on the INDIRECT docker_inspect "
      "wd- gate (no explicit gate in do_instance_input)",
      're.fullmatch(r"wd-[A-Za-z0-9_-]{1,32}"' in fn_src("docker_inspect"))

# ---------------------------------------------------------------- B. real function
print("== B. real do_instance_input (stubbed I/O) ==")
_state = {"wing_calls": [], "fifo_opens": [], "fifo_writes": [],
          "exec_calls": [], "container": "running", "wing_code": 200,
          "wing_resp": {"ok": True}, "exec_rc": 0, "fifo_fail": False}


def fake_wing(p, method, path, data=None, timeout=8, attempts=3):
    _state["wing_calls"].append((method, path, data))
    return _state["wing_code"], _state["wing_resp"]


def fake_container_status(cname):
    return _state["container"]


def fake_exec(name, cmd, timeout=20):
    _state["exec_calls"].append((name, cmd))
    return _state["exec_rc"], ""


def reset():
    _state["wing_calls"] = []
    _state["fifo_opens"] = []
    _state["fifo_writes"] = []
    _state["exec_calls"] = []
    _state["container"] = "running"
    _state["wing_code"] = 200
    _state["wing_resp"] = {"ok": True}
    _state["exec_rc"] = 0
    _state["fifo_fail"] = False
    app.wing_request = fake_wing
    app.load_providers = lambda: {"srt": {"url": "http://node:8090"}}
    app.container_status = fake_container_status
    app.waydroid_exec = fake_exec


_orig_open, _orig_write, _orig_close = os.open, os.write, os.close


def fake_open(path, flags):
    _state["fifo_opens"].append(path)
    if _state["fifo_fail"]:
        raise OSError("fifo missing")
    return 99


def fake_write(fd, data):
    _state["fifo_writes"].append(data)
    return len(data)


def fake_close(fd):
    return None


def call(name, provider, data):
    os.open, os.write, os.close = fake_open, fake_write, fake_close
    try:
        return app.do_instance_input(name, provider, data)
    finally:
        os.open, os.write, os.close = _orig_open, _orig_write, _orig_close


for n in BAD_NAMES:
    reset()
    ok_, res = call(n, "srt", {"type": "tap", "x": 500, "y": 500})
    check(f"remote hostile {n!r} -> instance not found, zero wing calls",
          (not ok_) and res.get("error") == "instance not found"
          and _state["wing_calls"] == [],
          f"ok={ok_} res={res} wing={_state['wing_calls']}")
for n in BAD_NAMES:
    reset()
    ok_, res = call(n, "local", {"type": "tap", "x": 500, "y": 500})
    check(f"local hostile {n!r} -> instance not found, zero FIFO/exec",
          (not ok_) and res.get("error") == "instance not found"
          and _state["fifo_opens"] == [] and _state["exec_calls"] == [],
          f"ok={ok_} res={res} fifo={_state['fifo_opens']} "
          f"exec={_state['exec_calls']}")

reset()
ok_, res = call("rbx1", "srt", {"type": "tap", "x": 500, "y": 500})
check("valid remote tap proxies with the exact wing URL + payload",
      ok_ and _state["wing_calls"]
      == [("POST", "/api/agent/instances/rbx1/input",
           {"type": "tap", "x": 500, "y": 500})],
      f"ok={ok_} wing={_state['wing_calls']}")
reset()
_state["wing_code"] = 500
_state["wing_resp"] = {"error": "wing failed"}
ok_, res = call("rbx1", "srt", {"type": "tap", "x": 500, "y": 500})
check("valid remote tap with wing 500 -> False, resp passthrough",
      (not ok_) and res.get("error") == "wing failed",
      f"ok={ok_} res={res}")
reset()
app.load_providers = lambda: {}
ok_, res = call("rbx1", "srt", {"type": "tap", "x": 500, "y": 500})
check("valid remote tap, node missing -> node not found, zero wing",
      (not ok_) and res.get("error") == "node not found"
      and _state["wing_calls"] == [],
      f"ok={ok_} res={res} wing={_state['wing_calls']}")
reset()
ok_, res = call("rbx1", "local", {"type": "tap", "x": 500, "y": 500})
check("valid local tap writes the exact FIFO path + scaled command",
      ok_ and res.get("ok") is True
      and _state["fifo_opens"]
      == ["/var/lib/docker/volumes/wd-rbx1-data/_data/data/media/0/"
          "jerusalem/input.fifo"]
      and _state["fifo_writes"] == [b"input tap 512 304\n"]
      and _state["exec_calls"] == [],
      f"ok={ok_} res={res} fifo={_state['fifo_opens']} "
      f"writes={_state['fifo_writes']} exec={_state['exec_calls']}")
reset()
_state["container"] = "stopped"
ok_, res = call("rbx1", "local", {"type": "tap", "x": 500, "y": 500})
check("valid local tap, stopped -> instance not running, zero FIFO/exec",
      (not ok_) and res.get("error") == "instance not running"
      and _state["fifo_opens"] == [] and _state["exec_calls"] == [],
      f"ok={ok_} res={res}")
reset()
_state["fifo_fail"] = True
ok_, res = call("rbx1", "local", {"type": "tap", "x": 500, "y": 500})
check("valid local tap, FIFO missing -> waydroid_exec fallback, ok",
      ok_ and _state["exec_calls"] == [("rbx1", "input tap 512 304")],
      f"ok={ok_} exec={_state['exec_calls']}")
reset()
_state["fifo_fail"] = True
_state["exec_rc"] = 1
ok_, res = call("rbx1", "local", {"type": "tap", "x": 500, "y": 500})
check("valid local tap, FIFO missing + exec fail -> input failed",
      (not ok_) and res.get("error") == "input failed",
      f"ok={ok_} res={res}")
reset()
ok_, res = call("rbx1", "local", {"type": "bogus"})
check("unknown input type unchanged -> unknown input type, zero FIFO/exec",
      (not ok_) and res.get("error") == "unknown input type"
      and _state["fifo_opens"] == [] and _state["exec_calls"] == [],
      f"ok={ok_} res={res}")
reset()
ok_, res = call("rbx1", "local", {"type": "tap", "x": 5000, "y": 500})
check("out-of-range tap unchanged -> x/y required, zero FIFO/exec",
      (not ok_) and res.get("error") == "x/y required"
      and _state["fifo_opens"] == [] and _state["exec_calls"] == [],
      f"ok={ok_} res={res}")

# ---------------------------------------------------------------- C. contracts
print("== C. source contracts ==")
src = fn_src("do_instance_input")
check("gate is the first statement of the function (before provider branch)",
      src.index("valid_instance_name(name)") < src.index('if provider != "local":'))
check("gate precedes the remote wing_request call",
      src.index("valid_instance_name(name)") < src.index("wing_request(p,"))
check("gate precedes the local FIFO path build",
      src.index("valid_instance_name(name)") < src.index("fifo_path ="))
check("gate returns the route-style error, not a new shape",
      'return False, {"error": "instance not found"}' in src)
check("waydroid_exec sink gate kept",
      "if not valid_instance_name(name):" in fn_src("waydroid_exec"))
check("capture_screenshot sink gate kept",
      "if not valid_instance_name(name):" in fn_src("capture_screenshot"))
check("instance_single_action sink gate kept",
      "if not valid_instance_name(name):" in fn_src("instance_single_action"))
check("login sink gate kept",
      "if not valid_instance_name(name):" in fn_src("_roblox_login_inner"))
check("input + webrtc offer routes still gate too (defence in depth)",
      app_src.count("valid_instance_name(name)") >= 10)
check("remote-autologin app.py snapshot == manager/app.py",
      open(SNAP_APP, encoding="utf-8").read() == app_src)

# ---------------------------------------------------------------- D. negative proof
print("== D. negative proof ==")


def old_remote_flow(name, provider, data):
    """Reconstruction of the pre-fix remote flow: no name gate."""
    p = {"url": "http://node:8090"}
    return fake_wing(p, "POST", f"/api/agent/instances/{name}/input",
                     data, timeout=15)


reset()
old_remote_flow("rbx1;id>/tmp/p", "srt", {"type": "tap"})
check("pre-fix remote flow DOES reach wing with the hostile URL",
      len(_state["wing_calls"]) == 1
      and _state["wing_calls"][0][1]
      == "/api/agent/instances/rbx1;id>/tmp/p/input",
      f"wing={_state['wing_calls']}")

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
