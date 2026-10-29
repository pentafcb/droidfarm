#!/usr/bin/env python3
"""Tests for the agent status/delete sink gates (agent-status-delete-sink-gate).

Census finding (2026-10-28): the wing agent's sink-gate audit was nearly
closed — create, resize, screenshot, input, launch, focus, b64 staging,
stream_wanted, drift, degraded, and every selfwatch helper already fail
closed on invalid names at the sink — but TWO sinks were route-gated only:

  A. instance_status_linux(): the /status route gates names, but the
     /api/agent/instances loop calls cached_instance_status_linux() on
     load_instances() keys with NO route gate between them, and invalid
     names bypass that cache and call through here. A corrupted
     instances.json key therefore reached this sink on every 10s manager
     poll, building a volume path from the raw name (a ../ traversal is
     an existence probe of another instance's markers) and running a
     docker inspect on wd-<name> before the (gated) drift/degraded
     readers ever ran.
  B. delete_instance_linux(): the DELETE route gates names, but the sink
     itself drove docker rm / volume rm / state pop from the raw name —
     a future internal caller passing a corrupted key would delete
     before any route gate ever ran.

Fix (wing/agent.py only, no manager/dashboard/wire change, no VERSION
bump — the manager's agent gate is >= 1.0.2, and bumping would churn
53 test files' VERSION assertions for zero behavioral gain):
  A. instance_status_linux() fails closed at the top: invalid name ->
     the stopped-shaped dict, with zero fs/docker touches.
  B. delete_instance_linux() fails closed at the top: invalid name ->
     False, with zero docker/state calls (old code always returned True).

Covers:
  A. regression proof (old flow builds a volume path outside the
     instance's own volume for a traversal name)
  B. status hostile-name matrix -> stopped dict, zero docker calls
  C. status valid stopped/running paths unchanged (exact dict shape,
     exact docker calls, mem/cpu from stats)
  D. cached wrapper: invalid name -> stopped dict, no cache entry;
     valid name cached (second call makes no new docker calls)
  E. delete hostile-name matrix -> False, zero docker/state calls
  F. delete valid name -> True, exact docker rm + volume rm calls,
     state pop + save, selfwatch markers dropped
  G. gate placement (source: gate precedes first docker/fs call in
     both sinks) + sibling sink gates kept + route gates kept
  H. deploy wiring: agent.py already in the apply-all stage list,
     VERSION deliberately unchanged at 1.0.45
  I. negative proof (pre-fix flow probes outside the instance volume)
"""
import importlib.util
import inspect
import os
import sys
import tempfile

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


agent = load("sdsg_agent", AGENT)

HOSTILE = [
    "../rbx1", "../../etc", "a/b", "rbx1;rm", "rbx1|cat", "rbx1`id`",
    "rbx1$(id)", "rbx 1", "", "a" * 33, "rbx1\n", "rbx1\x00", None,
    123, ["rbx1"], "rbx1%", "..", ".",
]

STOPPED_SHAPE = {"running": False, "setup": "stopped", "roblox": False,
                 "mem_used_mb": None, "cpu_pct": None,
                 "roblox_drift": False, "roblox_drift_reason": None,
                 "degraded": False, "degraded_reason": None}


class FakeDocker:
    """Stand-in for agent.docker: records calls, simulates one container."""

    def __init__(self, running=True):
        self.calls = []
        self.running = running

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[0] == "ps":
            if self.running:
                return 0, "wd-rbx1\tUp 5 minutes"
            return 0, ""
        if args[0] == "inspect":
            return 0, "2026-10-28T10:00:00Z"
        if args[0] == "stats":
            return 0, "wd-rbx1\t512MiB / 2GiB\t12.5%"
        if args[0] in ("rm", "volume", "start", "stop", "update"):
            return 0, ""
        return 0, ""


def reset_res_cache():
    try:
        agent._inst_res["ts"] = 0
        agent._inst_res["ok_ts"] = 0
        agent._inst_res["data"] = {}
    except Exception:
        pass


def reset_status_cache():
    try:
        agent._agent_status_cache.clear()
    except Exception:
        pass


# --- A. regression proof: the pre-fix flow resolves into a DIFFERENT volume
for h in ("../rbx1", "../../etc", "a/b"):
    base = "/var/lib/docker/volumes"
    built = os.path.normpath(os.path.join(
        base, "wd-" + h + "-data", "_data"))
    vol_dir = os.path.relpath(built, base).split(os.sep)[0]
    check(vol_dir != "wd-" + h + "-data",
          f"regression: traversal name {h!r} resolves into volume {vol_dir!r}",
          built)

# --- B. status hostile-name matrix ---------------------------------------
tmp = tempfile.mkdtemp()
agent.SELF_WATCH_DIR = tmp
for h in HOSTILE:
    fd = FakeDocker()
    agent.docker = fd
    reset_res_cache()
    reset_status_cache()
    try:
        out = agent.instance_status_linux(h)
    except Exception as e:
        bad(f"status hostile {h!r} raises", repr(e))
        continue
    expect = dict(STOPPED_SHAPE)
    expect["name"] = h
    check(out == expect, f"status hostile {h!r} -> stopped dict",
          repr(out))
    check(fd.calls == [], f"status hostile {h!r} -> zero docker calls",
          repr(fd.calls))

# --- C. status valid paths unchanged --------------------------------------
fd = FakeDocker(running=False)
agent.docker = fd
reset_res_cache()
out = agent.instance_status_linux("rbx1")
expect = dict(STOPPED_SHAPE)
expect["name"] = "rbx1"
check(out == expect, "status valid stopped -> stopped dict", repr(out))
check(any(c[0] == "ps" for c in fd.calls),
      "status valid stopped -> docker ps consulted", repr(fd.calls))

fd = FakeDocker(running=True)
agent.docker = fd
reset_res_cache()
out = agent.instance_status_linux("rbx1")
check(out.get("running") is True, "status valid running -> running",
      repr(out))
check(out.get("setup") == "booting",
      "status valid running, no markers -> booting", repr(out))
check(out.get("mem_used_mb") == 512.0 and out.get("cpu_pct") == 12.5,
      "status valid running -> mem/cpu from stats", repr(out))
check(out.get("started_at") == 1793181600,
      "status valid running -> started_at parsed", repr(out.get("started_at")))
check(out.get("roblox_drift") is False and out.get("degraded") is False,
      "status valid running -> drift/degraded false", repr(out))

# --- D. cached wrapper ------------------------------------------------------
fd = FakeDocker()
agent.docker = fd
reset_res_cache()
reset_status_cache()
out1 = agent.cached_instance_status_linux("../rbx1")
check(out1.get("running") is False and out1.get("setup") == "stopped",
      "cached hostile -> stopped dict", repr(out1))
check(agent._agent_status_cache == {},
      "cached hostile -> no cache entry", repr(agent._agent_status_cache))

fd = FakeDocker(running=True)
agent.docker = fd
reset_res_cache()
reset_status_cache()
out1 = agent.cached_instance_status_linux("rbx1")
n_calls = len(fd.calls)
out2 = agent.cached_instance_status_linux("rbx1")
check(out1 == out2, "cached valid -> identical dicts", repr((out1, out2)))
check(len(fd.calls) == n_calls,
      "cached valid -> second call makes no new docker calls",
      repr(fd.calls))

# --- E. delete hostile-name matrix -----------------------------------------
saved_state = {}

def fake_load():
    return dict(saved_state)

def fake_save(data):
    saved_state.clear()
    saved_state.update(data)

agent.load_instances = fake_load
agent.save_instances = fake_save
for h in HOSTILE:
    saved_state.clear()
    saved_state.update({"rbx1": {"instance_id": 1}})
    fd = FakeDocker()
    agent.docker = fd
    try:
        out = agent.delete_instance_linux(h)
    except Exception as e:
        bad(f"delete hostile {h!r} raises", repr(e))
        continue
    check(out is False, f"delete hostile {h!r} -> False", repr(out))
    check(fd.calls == [], f"delete hostile {h!r} -> zero docker calls",
          repr(fd.calls))
    check(saved_state == {"rbx1": {"instance_id": 1}},
          f"delete hostile {h!r} -> state untouched", repr(saved_state))

# --- F. delete valid name ---------------------------------------------------
saved_state.clear()
saved_state.update({"rbx1": {"instance_id": 1}, "rbx2": {"instance_id": 2}})
fd = FakeDocker()
agent.docker = fd
marker = os.path.join(tmp, "rbx1.degraded")
with open(marker, "w") as fh:
    fh.write("test reason\n")
out = agent.delete_instance_linux("rbx1")
check(out is True, "delete valid -> True", repr(out))
check(("rm", "-f", "wd-rbx1") in fd.calls,
      "delete valid -> docker rm -f wd-rbx1", repr(fd.calls))
check(("volume", "rm", "wd-rbx1-data") in fd.calls and
      ("volume", "rm", "wd-rbx1-varlib") in fd.calls,
      "delete valid -> both volumes removed", repr(fd.calls))
check(saved_state == {"rbx2": {"instance_id": 2}},
      "delete valid -> state popped + saved", repr(saved_state))
check(not os.path.exists(marker),
      "delete valid -> selfwatch markers dropped", marker)

# --- G. gate placement + sibling/route gates kept ---------------------------
src_status = inspect.getsource(agent.instance_status_linux)
src_delete = inspect.getsource(agent.delete_instance_linux)
check(src_status.index("valid_agent_instance_name") <
      src_status.index("container_status"),
      "gate placement: status gate precedes container_status")
check(src_delete.index("valid_agent_instance_name") <
      src_delete.index('docker("rm"'),
      "gate placement: delete gate precedes docker rm")
for fn, hostile_ret, extra in (
        (agent.screenshot_bytes_linux, None, ()),
        (agent.input_linux, False, ("input tap 1 1",)),
        (agent.launch_roblox_linux, False, ()),
        (agent.roblox_focused_linux, None, ()),
        (agent.check_roblox_drift, None, ()),
        (agent.selfwatch_check_instance, True, ())):
    fd = FakeDocker()
    agent.docker = fd
    try:
        if fn is agent.check_roblox_drift:
            got = fn("../rbx1", st_dir=tmp)
        else:
            got = fn("../rbx1", *extra)
        check(got == hostile_ret or got is hostile_ret,
              f"sibling gate kept: {fn.__name__} hostile -> {hostile_ret!r}",
              repr(got))
    except Exception as e:
        bad(f"sibling gate kept: {fn.__name__} raises", repr(e))
src_all = open(AGENT, encoding="utf-8").read()
check(src_all.count("valid_agent_instance_name(name)") >= 8,
      "route gates kept: name gate used across routes/sinks",
      str(src_all.count("valid_agent_instance_name(name)")))

# --- H. deploy wiring ---------------------------------------------------------
deploy = open(DEPLOY_ALL, encoding="utf-8").read()
check("wing/agent.py" in deploy,
      "deploy wiring: agent.py already in apply-all stage list")
check(agent.VERSION == "1.0.45",
      "VERSION deliberately unchanged at 1.0.45 (no bump needed)",
      repr(agent.VERSION))

# --- I. negative proof --------------------------------------------------------
built = os.path.join("/var/lib/docker/volumes", "wd-" + "../rbx1" + "-data",
                      "_data", "data", "media", "0", "jerusalem", "live.png")
check("wd-rbx1-data" not in built and "rbx1-data" in built,
      "negative proof: pre-fix status path resolves into another volume",
      built)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
