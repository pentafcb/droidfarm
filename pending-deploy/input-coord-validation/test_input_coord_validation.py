#!/usr/bin/env python3
"""Tests for input-coord-validation (manager scale() + agent 1.0.43 tap path).

GAP: viewer tap/swipe coordinates are normalized 0..1000 by the dashboard,
but neither server validated them. Python's json.loads accepts Infinity
and NaN, so a crafted tap with x=Infinity reached int(float(x)): int(inf)
raises OverflowError, which neither except clause caught (both caught only
TypeError/ValueError) — the request thread died and the connection dropped
with NO response instead of a clean 400. Out-of-range coords (e.g. 1500)
were likewise passed straight through to `input tap`.

FIX (same contract both sides):
  * manager/app.py do_instance_input scale(): float-convert, then reject
    non-finite and out-of-range coords (NaN fails every comparison, inf
    fails the upper bound) -> None -> clean "x/y required" 400. Tap and
    swipe share scale(), so both are covered.
  * wing/agent.py: new _agent_tap_cmd(data) helper (same shape as the
    existing _agent_text_cmd) with the same validation; the do_POST tap
    branch now calls it, so a bad tap is a clean 400 "invalid input".
  * wing agent 1.0.42 -> 1.0.43; VERSION tuples widened, exact pins moved.

Covers:
  A. versions: agent 1.0.43 + snapshot parity + manager gate compat
  B. manager do_instance_input end-to-end (stubbed container + exec):
     valid taps/swipe/key incl. boundary + truncation semantics
  C. manager invalid coords -> clean 400, exec never called; regression
     proof that int(inf) raises OverflowError (not a ValueError, so the
     old handler necessarily missed it)
  D. agent _agent_tap_cmd direct: valid + invalid matrix
  E. source contracts: range check present, tap branch uses the helper,
     no unguarded int(float( left in either input path, dashboard still
     clamps 0..1000 before sending
"""
import importlib.util
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")
SNAP_AGENT = os.path.join(REPO, "pending-deploy", "remote-autologin", "agent.py")
SNAP_APP = os.path.join(REPO, "pending-deploy", "remote-autologin", "app.py")
INDEX = os.path.join(REPO, "manager", "static", "index.html")

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


agent = load("icv_agent", AGENT)
app = load("icv_app", APP)
ag_src = open(AGENT, encoding="utf-8").read()
app_src = open(APP, encoding="utf-8").read()
idx_src = open(INDEX, encoding="utf-8").read()

print("== A. versions ==")
check("agent VERSION is 1.0.45", agent.VERSION == "1.0.45", repr(agent.VERSION))
snap = load("icv_snap", SNAP_AGENT)
check("remote-autologin agent.py snapshot matches canonical VERSION",
      snap.VERSION == agent.VERSION, repr(snap.VERSION))
check("remote-autologin app.py snapshot carries the new scale check",
      "0 <= fx <= 1000" in open(SNAP_APP, encoding="utf-8").read())
check("manager version gate accepts 1.0.43",
      app._version_at_least("1.0.43", "1.0.2"))

print("== B. manager do_instance_input, valid inputs ==")
calls = []


def fake_status(name):
    return "running"


def fake_exec(name, cmd, timeout=20):
    calls.append((name, cmd))
    return (0, "")


orig_status, orig_exec = app.container_status, app.waydroid_exec
app.container_status, app.waydroid_exec = fake_status, fake_exec
try:
    def run_tap(x, y):
        before = len(calls)
        ok_flag, res = app.do_instance_input("rbx1", "local",
                                             {"type": "tap", "x": x, "y": y})
        return ok_flag, res, calls[before:]

    ok_flag, res, new = run_tap(0, 0)
    check("tap (0,0) -> input tap 0 0",
          ok_flag and new == [("rbx1", "input tap 0 0")], repr((res, new)))
    ok_flag, res, new = run_tap(500, 500)
    check("tap (500,500) -> input tap 512 304",
          ok_flag and new == [("rbx1", "input tap 512 304")], repr((res, new)))
    ok_flag, res, new = run_tap(1000, 1000)
    check("tap (1000,1000) -> input tap 1024 608",
          ok_flag and new == [("rbx1", "input tap 1024 608")], repr((res, new)))
    ok_flag, res, new = run_tap("250", "750")
    check("tap string numerics -> input tap 256 456",
          ok_flag and new == [("rbx1", "input tap 256 456")], repr((res, new)))
    ok_flag, res, new = run_tap(100, 100)
    check("tap (100,100) truncates -> input tap 102 60",
          ok_flag and new == [("rbx1", "input tap 102 60")], repr((res, new)))
    before = len(calls)
    ok_flag, res = app.do_instance_input(
        "rbx1", "local",
        {"type": "swipe", "x1": 0, "y1": 0, "x2": 1000, "y2": 1000})
    check("swipe (0,0,1000,1000) -> input swipe 0 0 1024 608",
          ok_flag and calls[before:] == [("rbx1", "input swipe 0 0 1024 608")],
          repr((res, calls[before:])))
    before = len(calls)
    ok_flag, res = app.do_instance_input("rbx1", "local",
                                         {"type": "key", "key": "HOME"})
    check("key HOME untouched -> input keyevent 3",
          ok_flag and calls[before:] == [("rbx1", "input keyevent 3")],
          repr((res, calls[before:])))

    print("== C. manager invalid coords -> clean 400, exec never called ==")
    inf, nan = float("inf"), float("nan")
    for label, x, y in [("inf", inf, 500), ("-inf", -inf, 500),
                        ("nan", nan, 500), ("Infinity string", "Infinity", 500),
                        ("1001", 1001, 500), ("-1", -1, 500),
                        ("missing y", 500, None), ("abc", "abc", 500)]:
        before = len(calls)
        ok_flag, res = app.do_instance_input("rbx1", "local",
                                             {"type": "tap", "x": x, "y": y})
        check(f"tap {label} -> 400 x/y required, exec not called",
              (not ok_flag) and res.get("error") == "x/y required"
              and len(calls) == before, repr((ok_flag, res, len(calls) - before)))
    before = len(calls)
    ok_flag, res = app.do_instance_input(
        "rbx1", "local",
        {"type": "swipe", "x1": 0, "y1": 0, "x2": inf, "y2": 1000})
    check("swipe inf x2 -> 400 x1/y1/x2/y2 required, exec not called",
          (not ok_flag) and res.get("error") == "x1/y1/x2/y2 required"
          and len(calls) == before, repr((ok_flag, res)))
    try:
        int(float("inf"))
        raised = None
    except OverflowError:
        raised = "OverflowError"
    except ValueError:
        raised = "ValueError"
    check("regression proof: int(inf) raises OverflowError, not ValueError",
          raised == "OverflowError" and not issubclass(OverflowError, ValueError),
          repr(raised))
finally:
    app.container_status, app.waydroid_exec = orig_status, orig_exec

print("== D. agent _agent_tap_cmd ==")
check("agent tap (0,0) -> input tap 0 0",
      agent._agent_tap_cmd({"x": 0, "y": 0}) == "input tap 0 0")
check("agent tap (500,500) -> input tap 512 304",
      agent._agent_tap_cmd({"x": 500, "y": 500}) == "input tap 512 304")
check("agent tap (1000,1000) -> input tap 1024 608",
      agent._agent_tap_cmd({"x": 1000, "y": 1000}) == "input tap 1024 608")
check("agent tap string numerics -> input tap 256 456",
      agent._agent_tap_cmd({"x": "250", "y": "750"}) == "input tap 256 456")
for label, x, y in [("inf", inf, 500), ("-inf", -inf, 500),
                    ("nan", nan, 500), ("Infinity string", "Infinity", 500),
                    ("1001", 1001, 500), ("-1", -1, 500),
                    ("None", None, 500), ("abc", "abc", 500)]:
    check(f"agent tap {label} -> None",
          agent._agent_tap_cmd({"x": x, "y": y}) is None)

print("== E. source contracts ==")
check("manager scale() carries the 0..1000 range check",
      "0 <= fx <= 1000" in app_src)
check("agent tap branch calls _agent_tap_cmd",
      "cmd = _agent_tap_cmd(data)" in ag_src)
check("manager has no int(float( left in the input path",
      "int(float(" not in app_src)
ag_intfloat = [ln for ln in ag_src.splitlines() if "int(float(" in ln]
check("agent int(float( survives only in guarded _sw_read_uptime_s",
      len(ag_intfloat) == 1 and "parts[0]" in ag_intfloat[0], repr(ag_intfloat))
check("dashboard still clamps taps to 0..1000 before sending",
      "tx>=0 && tx<=1000" in idx_src)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
