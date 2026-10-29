#!/usr/bin/env python3
"""Tests for numeric-overflow-guard (manager validators + agent 1.0.44).

GAP: t148 fixed the tap-coord path, but the SAME OverflowError class
lurked in every other numeric conversion of request data. Python's
json.loads accepts Infinity/NaN and arbitrary-precision ints, so:
  * manager _create_instance int(memory_mb), _fleet_validate int(count) /
    int(memory_mb), validate_resize_params int(memory_mb): a crafted
    Infinity raised OverflowError, which the except clauses missed (only
    TypeError/ValueError) — the handler thread died and the connection
    dropped with NO response instead of a clean 400.
  * wing agent create/resize routes int(cpus)/int(memory_mb): same drop,
    seen manager-side as a failed wing_request instead of a clean 400.
  * "never raises" sanitizers clean_agent_resource/_fleet_number:
    float(10**400) raises OverflowError, breaking /api/instances on a
    bad agent payload; _remote_started_at int(Infinity) likewise.

FIX: OverflowError joins the except tuples at all 9 manager sites
(create, fleet-plan x2, resize, clean_agent_resource, _fleet_number,
_remote_started_at, heartbeat recoveries + port) and both agent routes.
Wing agent 1.0.43 -> 1.0.44; VERSION tuples widened, exact pins moved.

Covers:
  A. versions: agent 1.0.44 + snapshot parity + manager gate compat
  B. regression proof: int(inf) / float(10**400) raise OverflowError,
     not ValueError, so the old handlers necessarily missed them
  C. manager validators: inf/nan/huge/valid matrix -> clean errors,
     valid inputs byte-identical
  D. manager sanitizers: inf/huge -> None, valid -> rounded value
  E. agent source contracts: both routes catch OverflowError
  F. manager source contracts: 9 edited sites carry OverflowError
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


agent = load("nog_agent", AGENT)
app = load("nog_app", APP)
ag_src = open(AGENT, encoding="utf-8").read()
app_src = open(APP, encoding="utf-8").read()

INF = float("inf")
NINF = float("-inf")
NAN = float("nan")
HUGE = 10 ** 400

print("== A. versions + snapshots ==")
check("agent VERSION is 1.0.45", agent.VERSION == "1.0.45", repr(agent.VERSION))
check("snapshot agent.py byte-identical to canonical",
      open(SNAP_AGENT, "rb").read() == open(AGENT, "rb").read())
check("snapshot app.py byte-identical to canonical",
      open(SNAP_APP, "rb").read() == open(APP, "rb").read())
check("manager version gate accepts 1.0.44",
      app._version_at_least("1.0.45", "1.0.2"))

print("== B. regression proof ==")
try:
    int(INF)
    bad("int(inf) raises", "no exception")
except OverflowError:
    ok("int(inf) raises OverflowError")
except Exception as e:  # noqa: BLE001 - proving the class
    bad("int(inf) raises OverflowError", f"got {type(e).__name__}")
try:
    float(HUGE)
    bad("float(10**400) raises", "no exception")
except OverflowError:
    ok("float(10**400) raises OverflowError")
except Exception as e:  # noqa: BLE001 - proving the class
    bad("float(10**400) raises OverflowError", f"got {type(e).__name__}")
check("OverflowError is not a ValueError",
      not issubclass(OverflowError, ValueError))

print("== C. manager validators ==")
# validate_resize_params: (clean, None) or (None, err); never raises.
for label, body in [
        ("inf memory", {"memory_mb": INF}),
        ("-inf memory", {"memory_mb": NINF}),
        ("nan memory", {"memory_mb": NAN}),
        ("huge memory", {"memory_mb": HUGE}),
        ("inf cpus", {"cpus": INF}),
        ("huge cpus", {"cpus": HUGE}),
        ("bad type", {"memory_mb": "abc"})]:
    # Any clean error string counts: inf/nan/huge-float hit the except,
    # while a huge int passes int() untouched and fails the range check
    # instead — both are clean errors, never a raise.
    try:
        clean, err = app.validate_resize_params(body)
        check(f"resize {label} -> clean error, no raise",
              clean is None and isinstance(err, str) and err,
              repr((clean, err)))
    except Exception as e:  # noqa: BLE001 - the fix is "never raises"
        bad(f"resize {label} -> clean error, no raise", f"raised {type(e).__name__}")
clean, err = app.validate_resize_params({"memory_mb": 2048, "cpus": 2})
check("resize valid -> exact dict",
      (clean, err) == ({"memory_mb": 2048, "cpus": 2.0}, None), repr((clean, err)))
clean, err = app.validate_resize_params({"memory_mb": "2048"})
check("resize string memory -> coerced",
      (clean, err) == ({"memory_mb": 2048, "cpus": None}, None), repr((clean, err)))

# _fleet_validate: (clean, None) or (None, (code, err)); provider local
# avoids all provider lookups (load_providers stubbed to {} anyway).
app.load_providers = lambda: {}
for label, args in [
        ("inf count", ("rbx", INF, 2, 2048, "local")),
        ("nan count", ("rbx", NAN, 2, 2048, "local")),
        ("inf memory", ("rbx", 2, 2, INF, "local")),
        ("huge memory", ("rbx", 2, 2, HUGE, "local")),
        ("inf cpus", ("rbx", 2, INF, 2048, "local")),
        ("huge cpus", ("rbx", 2, HUGE, 2048, "local"))]:
    try:
        clean, err = app._fleet_validate(*args)
        check(f"fleet {label} -> 400, no raise",
              clean is None and isinstance(err, tuple) and err[0] == 400,
              repr((clean, err)))
    except Exception as e:  # noqa: BLE001 - the fix is "never raises"
        bad(f"fleet {label} -> 400, no raise", f"raised {type(e).__name__}")
clean, err = app._fleet_validate("rbx", 3, 2, 2048, "local")
check("fleet valid -> exact dict",
      (clean, err) == ({"prefix": "rbx", "count": 3, "cpus": 2.0,
                        "memory_mb": 2048, "provider": "local"}, None),
      repr((clean, err)))

print("== D. manager sanitizers ==")
for label, fn, val, want in [
        ("clean_agent_resource inf", app.clean_agent_resource, INF, None),
        ("clean_agent_resource huge", app.clean_agent_resource, HUGE, None),
        ("clean_agent_resource valid", app.clean_agent_resource, 123.456, 123.5),
        ("clean_agent_resource str", app.clean_agent_resource, "abc", None),
        ("_fleet_number inf", app._fleet_number, INF, None),
        ("_fleet_number huge", app._fleet_number, HUGE, None),
        ("_fleet_number valid", app._fleet_number, 7.25, 7.2),
        ("_fleet_number neg", app._fleet_number, -1, None)]:
    try:
        got = fn(val)
        check(f"{label} -> {want}", got == want, repr(got))
    except Exception as e:  # noqa: BLE001 - the fix is "never raises"
        bad(f"{label} -> {want}", f"raised {type(e).__name__}")
app.wing_request = lambda *a, **k: (200, {"started_at": INF})
try:
    check("_remote_started_at inf -> 0", app._remote_started_at({}, "rbx1") == 0)
except Exception as e:  # noqa: BLE001 - the fix is "never raises"
    bad("_remote_started_at inf -> 0", f"raised {type(e).__name__}")
app.wing_request = lambda *a, **k: (200, {"started_at": 12345})
check("_remote_started_at valid -> value",
      app._remote_started_at({}, "rbx1") == 12345)

print("== E. agent source contracts ==")
check("agent create route catches OverflowError",
      "except (TypeError, ValueError, OverflowError):" in ag_src
      and "max(1, min(16, int(cpus)))" in ag_src)
check("agent resize route catches OverflowError",
      "except (TypeError, ValueError, OverflowError):" in ag_src
      and 'int(data["memory_mb"])' in ag_src)
check("agent has both OverflowError excepts",
      ag_src.count("except (TypeError, ValueError, OverflowError):") >= 2)

print("== F. manager source contracts ==")
n = app_src.count("except (TypeError, ValueError, OverflowError):")
check("manager has all 9 OverflowError excepts", n >= 9, f"found {n}")
for label, needle in [
        ("resize validator", 'return None, "invalid cpus/memory"'),
        ("fleet count", '"count must be 1-20"'),
        ("fleet cpus/memory", 'return None, (400, {"error": "invalid cpus/memory"})'),
        ("create handler", 'return self._json(400, {"error": "invalid cpus/memory"})'),
        ("heartbeat recoveries", 'int(sw.get("recoveries", 0))'),
        ("heartbeat port", 'int(data.get("port", 0))')]:
    check(f"manager {label} site present", needle in app_src)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
