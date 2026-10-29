#!/usr/bin/env python3
"""Tests for the agent resize-numerics sink gate (agent-resize-numerics-sink-gate).

Census finding (2026-10-28): the agent-side sink census (t170) closed the
NAME sinks, t171 closed the create numerics sink, and the t171 session
named resize as the adjacent ungated numerics sink — `resize_instance_linux()`
took memory_mb/cpus straight into the docker update argv and the node
state record. Its route coerces + bounds today (memory 1024-32768 MB,
cpus 0.5-32, both-None refused), but the sink itself validated nothing:
a future internal caller passing an uncoercible, negative,
fractional-memory, or absurd value would drive docker/state before any
route gate ever ran. (List-form docker argv means no shell injection
here — the failure mode is absurd limits and corrupted state, e.g.
memory 999999999 or cpus "2;rm" stored verbatim.)

Fix (wing/agent.py only, no manager/dashboard/wire change, no VERSION
bump — the manager's agent gate is >= 1.0.2, and bumping would churn 53
test files' VERSION assertions for zero behavioral gain): the sink
coerces and bounds-checks against the route's validator exactly
(memory 1024-32768 MB, cpus 0.5-32), failing closed with
(False, "invalid cpus/memory") — or (False, "memory_mb or cpus
required") when both are None — before any docker/state touch.
Coercion is type-preserving for cpus (int stays int, float stays
float, strings coerce to float), so the valid-path argv and state
record are byte-identical to today for every caller — a blanket
float() coercion broke direct int callers ("1" -> "1.0"), which the
t100 instance-resize suite caught; the shrink-below-live-usage guard
(resize_usage_ok) stays route-only, as before.

Covers:
  A. regression proof (old flow embeds raw numerics verbatim in argv/state)
  B. hostile numerics matrix -> refused, zero docker calls, zero saves
  C. both-None refused
  D. valid paths unchanged (exact argv, coerced state, partial, boundaries)
  E. gate placement + sibling gates kept + route validator kept
  F. deploy wiring (agent.py staged, VERSION unchanged at 1.0.45)
  G. negative proof (pre-fix argv reaches docker verbatim)
"""
import importlib.util
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
DEPLOY_ALL = os.path.join(REPO, "pending-deploy", "apply-all", "deploy-all.sh")

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


agent = load("arnsg_agent", AGENT)

NAN = float("nan")
INF = float("inf")

# --- A. regression proof: the pre-fix flow embeds raw numerics verbatim --
old_argv = ["update", "--memory", f"{999999999}m", "--cpus", str("2;rm"),
            "wd-rbx1"]
check("2;rm" in old_argv and "999999999m" in old_argv,
      "regression: old argv embeds hostile numerics verbatim", repr(old_argv))
old_state = {"cpus": "2;rm", "memory_mb": 999999999}
check(old_state["cpus"] == "2;rm" and old_state["memory_mb"] == 999999999,
      "regression: old state stores hostile numerics verbatim", repr(old_state))
check(str(-1) == "-1" and str(0.5) == "0.5",
      "regression: old flow passes negative/fractional cpus through", "")


class FakeDocker:
    """Stand-in for agent.docker: records calls, simulates success."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        return 0, ""


def setup():
    fd = FakeDocker()
    saved = []
    agent.docker = fd
    agent.load_instances = lambda: {"rbx1": {"memory_mb": 1024, "cpus": 1}}
    agent.save_instances = lambda data: saved.append(
        {k: dict(v) for k, v in data.items()})
    return fd, saved


# --- B. hostile numerics matrix --------------------------------------------
HOSTILE_CPUS = [True, False, "abc", "2;rm", "", NAN, INF, -INF,
                0, -1, 0.49, 0.0, 32.5, 100, 10 ** 9]
for h in HOSTILE_CPUS:
    fd, saved = setup()
    try:
        out = agent.resize_instance_linux("rbx1", 2048, h)
    except Exception as e:
        bad(f"resize hostile cpus {h!r} raises", repr(e))
        continue
    check(out == (False, "invalid cpus/memory"),
          f"resize hostile cpus {h!r} -> refused", repr(out))
    check(fd.calls == [] and saved == [],
          f"resize hostile cpus {h!r} -> zero docker/state", repr(fd.calls))

HOSTILE_MEM = [True, False, "abc", "2048;rm", "", 2048.5, "2048.5",
               NAN, INF, -INF, 0, -512, 1023, 32769, 10 ** 9]
for h in HOSTILE_MEM:
    fd, saved = setup()
    try:
        out = agent.resize_instance_linux("rbx1", h, 2)
    except Exception as e:
        bad(f"resize hostile memory {h!r} raises", repr(e))
        continue
    check(out == (False, "invalid cpus/memory"),
          f"resize hostile memory {h!r} -> refused", repr(out))
    check(fd.calls == [] and saved == [],
          f"resize hostile memory {h!r} -> zero docker/state", repr(fd.calls))

# --- C. both-None refused ----------------------------------------------------
fd, saved = setup()
out = agent.resize_instance_linux("rbx1", None, None)
check(out == (False, "memory_mb or cpus required"),
      "resize both-None -> refused", repr(out))
check(fd.calls == [] and saved == [],
      "resize both-None -> zero docker/state", repr(fd.calls))

# --- D. valid paths unchanged ------------------------------------------------
fd, saved = setup()
out = agent.resize_instance_linux("rbx1", 2048, 2.0)
check(out == (True, "resized"), "resize valid (2048, 2.0) -> resized", repr(out))
check(len(fd.calls) == 1 and fd.calls[0] == (
    "update", "--memory", "2048m", "--cpus", "2.0", "wd-rbx1"),
    "resize valid -> exact docker argv", repr(fd.calls))
check(len(saved) == 1 and saved[0]["rbx1"]["memory_mb"] == 2048
      and saved[0]["rbx1"]["cpus"] == 2.0,
      "resize valid -> coerced state (2048, 2.0)", repr(saved))

fd, saved = setup()
out = agent.resize_instance_linux("rbx1", "2048", "2")
check(out == (True, "resized"), "resize string numerics -> resized", repr(out))
check(len(fd.calls) == 1 and fd.calls[0] == (
    "update", "--memory", "2048m", "--cpus", "2.0", "wd-rbx1"),
    "resize string numerics -> exact coerced argv", repr(fd.calls))
check(len(saved) == 1 and saved[0]["rbx1"]["memory_mb"] == 2048
      and saved[0]["rbx1"]["cpus"] == 2.0,
      "resize string numerics -> coerced state", repr(saved))

fd, saved = setup()
out = agent.resize_instance_linux("rbx1", 2048, 2.5)
check(out == (True, "resized"), "resize fractional cpus 2.5 -> resized", repr(out))
check(len(fd.calls) == 1 and fd.calls[0][4] == "2.5",
      "resize fractional cpus -> argv '2.5'", repr(fd.calls))
check(len(saved) == 1 and saved[0]["rbx1"]["cpus"] == 2.5,
      "resize fractional cpus -> state 2.5", repr(saved))

fd, saved = setup()
out = agent.resize_instance_linux("rbx1", 2048, None)
check(out == (True, "resized"), "resize memory-only -> resized", repr(out))
check(len(fd.calls) == 1 and fd.calls[0] == (
    "update", "--memory", "2048m", "wd-rbx1"),
    "resize memory-only -> argv has no --cpus", repr(fd.calls))
check(len(saved) == 1 and saved[0]["rbx1"]["memory_mb"] == 2048
      and saved[0]["rbx1"]["cpus"] == 1,
      "resize memory-only -> cpus untouched in state", repr(saved))

fd, saved = setup()
out = agent.resize_instance_linux("rbx1", None, 4.0)
check(out == (True, "resized"), "resize cpus-only -> resized", repr(out))
check(len(fd.calls) == 1 and fd.calls[0] == (
    "update", "--cpus", "4.0", "wd-rbx1"),
    "resize cpus-only -> argv has no --memory", repr(fd.calls))
check(len(saved) == 1 and saved[0]["rbx1"]["memory_mb"] == 1024
      and saved[0]["rbx1"]["cpus"] == 4.0,
      "resize cpus-only -> memory untouched in state", repr(saved))

fd, saved = setup()
out = agent.resize_instance_linux("rbx1", 1024, 0.5)
check(out == (True, "resized"), "resize lower boundary (1024, 0.5) -> resized",
      repr(out))
fd, saved = setup()
out = agent.resize_instance_linux("rbx1", 32768, 32)
check(out == (True, "resized"), "resize upper boundary (32768, 32) -> resized",
      repr(out))

fd, saved = setup()
out = agent.resize_instance_linux("rbx1", 2048, 2)
check(out == (True, "resized"), "resize int cpus 2 -> resized", repr(out))
check(len(fd.calls) == 1 and fd.calls[0][4] == "2",
      "resize int cpus -> argv int '2' preserved (t100 regression)",
      repr(fd.calls))
check(len(saved) == 1 and saved[0]["rbx1"]["cpus"] == 2
      and type(saved[0]["rbx1"]["cpus"]) is int,
      "resize int cpus -> state int 2 preserved", repr(saved))

# invalid name still gated first (sibling gate, unchanged)
fd, saved = setup()
out = agent.resize_instance_linux("../rbx1", 2048, 2)
check(out == (False, "invalid name"), "resize hostile name -> refused", repr(out))
check(fd.calls == [] and saved == [],
      "resize hostile name -> zero docker/state", repr(fd.calls))

# docker failure: no state write (unchanged behavior)
fd, saved = setup()
agent.docker = lambda *a, timeout=60: (fd.calls.append(a), (1, "boom"))[1]
out = agent.resize_instance_linux("rbx1", 2048, 2.0)
check(out[0] is False and "docker update failed" in out[1],
      "resize docker failure -> refused", repr(out))
check(saved == [], "resize docker failure -> no state write", repr(saved))

# --- E. gate placement + sibling gates kept ----------------------------------
src = inspect.getsource(agent.resize_instance_linux)
check(src.index("agent-resize-numerics-sink-gate") < src.index('args = ["update"]'),
      "gate placement: numerics gate precedes docker argv build", "")
check(src.index("invalid cpus/memory") < src.index('args = ["update"]'),
      "gate placement: refusal precedes docker argv build", "")
check("memory_mb or cpus required" in src,
      "gate placement: both-None refusal in sink", "")
check("agent-create-numerics-sink-gate" in inspect.getsource(
    agent.create_instance_linux),
    "sibling kept: create numerics sink gate", "")
check("valid_agent_instance_name(name)" in inspect.getsource(
    agent.instance_status_linux),
    "sibling kept: status sink name gate", "")
check("valid_agent_instance_name(name)" in inspect.getsource(
    agent.delete_instance_linux),
    "sibling kept: delete sink name gate", "")
route = open(AGENT, encoding="utf-8").read()
check('memory 1024-32768 MB' in route,
      "route validator kept: resize route still bounds memory", "")
check('cpus 0.5-32' in route,
      "route validator kept: resize route still bounds cpus", "")
check("resize_usage_ok(name, memory_mb)" in route,
      "route guard kept: shrink-below-usage check still on route", "")

# --- F. deploy wiring ----------------------------------------------------------
deploy = open(DEPLOY_ALL, encoding="utf-8").read()
check("wing/agent.py" in deploy,
      "deploy wiring: agent.py already in apply-all stage list")
check(agent.VERSION == "1.0.45",
      "VERSION deliberately unchanged at 1.0.45 (no bump needed)",
      repr(agent.VERSION))

# --- G. negative proof ---------------------------------------------------------
pre_argv = ["update", "--memory", f"{10 ** 9}m", "--cpus", str("0;rm"),
            "wd-rbx1"]
fd, saved = setup()
agent.docker = lambda *a, timeout=60: (fd.calls.append(a), (0, ""))[1]
agent.docker(*pre_argv)
check(any("0;rm" in c for c in fd.calls),
      "negative proof: pre-fix argv reaches docker verbatim", repr(fd.calls))

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
