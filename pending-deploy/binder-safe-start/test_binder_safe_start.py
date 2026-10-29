#!/usr/bin/env python3
"""Tests for binder-safe start (manager + wing agent 1.0.33).

REAL GAP: after a host reboot the binderfs pool (and the loop-device pool)
drops back to the stock set, while containers still bind-mount the specific
/dev/binderfs/binderN devices they were created with. The boot-recovery
automation (df-watchdog v14 section 0 on the main node, agent 1.0.32
sw_boot_recovery on wing nodes) repairs this in its own cron/thread path --
but the dashboard's Start/Restart buttons and the batch "Start all" /
"Restart all" call `docker start` directly. A start issued before or during
boot recovery makes docker auto-create EMPTY DIRECTORIES at the vanished
bind sources -> dead binder mounts -> a stuck-boot wedge that then burns
the stuck-boot circuit budget.

Fix: manager/app.py gains _prepare_pools_for_start() (fast single-listdir
check; rmdir of empty stub dirs; ensure_binder_devices() +
ensure_loop_devices() when needed; best-effort, never raises), called
before `docker start` in instance_single_action's local start/restart
paths. wing/agent.py 1.0.33 gains _sw_prepare_pools_for_start() (same shape
over sw_boot_binder_pool()), called in the /start endpoint before
docker("start", ...).

Run: python3 test_binder_safe_start.py
Hermetic: os.listdir/isdir/rmdir patched; module run()/docker faked.

Covers:
  A. manager _prepare_pools_for_start: fast path, missing device, listdir
     failure, empty/non-empty stub dirs, never-raises, loop floor wiring,
     zero-subprocess fast path
  B. instance_single_action wiring: start/restart call it in order,
     fail-open on raise, stop/login/unknown unaffected
  C. agent: VERSION 1.0.33, _sw_prepare_pools_for_start behavior, loop
     wiring, /start endpoint ordering contract
"""
import importlib.util
import inspect
import os
import sys
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
AGENT = os.path.join(REPO, "wing", "agent.py")

PASS = 0
FAIL = 0


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


app = load("bss_app", APP)
agent = load("bss_agent", AGENT)

FULL_POOL = ["binder"] + ["binder%d" % i for i in range(1, 64)]
STOCK_POOL = ["binder"] + ["binder%d" % i for i in range(1, 26)]  # ~post-reboot


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        return 0


print("== A. manager _prepare_pools_for_start ==")


def run_prepare(dirnames, isdir_names=(), rmdir_fail=(), ensure_rec=None,
                count_loops=64, loop_rec=None, listdir_exc=None,
                ensure_exc=None, count_exc=None):
    """Drive app._prepare_pools_for_start with faked fs + helpers."""
    ensure_rec = ensure_rec if ensure_rec is not None else Rec()
    loop_rec = loop_rec if loop_rec is not None else Rec()
    rm_done = []

    def fake_ensure():
        ensure_rec()
        if ensure_exc:
            raise ensure_exc

    def fake_count():
        if count_exc:
            raise count_exc
        return count_loops

    def fake_listdir(p):
        if listdir_exc:
            raise listdir_exc
        assert p == "/dev/binderfs", p
        return list(dirnames)

    def fake_isdir(p):
        base = os.path.basename(p)
        return base in isdir_names

    def fake_rmdir(p):
        base = os.path.basename(p)
        if base in rmdir_fail:
            raise OSError("non-empty")
        rm_done.append(base)

    def bomb_run(cmd, timeout=30):
        raise AssertionError("run() must not be called on the fast path: %r" % cmd)

    old = (app.ensure_binder_devices, app.count_loop_devices,
           app.ensure_loop_devices, app.run)
    app.ensure_binder_devices = fake_ensure
    app.count_loop_devices = fake_count
    app.ensure_loop_devices = lambda *a, **k: loop_rec()
    app.run = bomb_run
    try:
        with mock.patch.object(os, "listdir", fake_listdir), \
             mock.patch.object(os.path, "isdir", fake_isdir), \
             mock.patch.object(os, "rmdir", fake_rmdir):
            ret = app._prepare_pools_for_start()
    finally:
        (app.ensure_binder_devices, app.count_loop_devices,
         app.ensure_loop_devices, app.run) = old
    return ret, ensure_rec, loop_rec, rm_done


# A1: complete pool -> no ensure call, True, zero subprocesses (bomb run).
ret, ensure_rec, loop_rec, rm_done = run_prepare(FULL_POOL)
check(ret is True and not ensure_rec.calls and not loop_rec.calls
      and not rm_done, "A1 complete pool: fast path, no ensure, True")
# A2: post-reboot stock pool (binder63 missing) -> ensure called once.
ret, ensure_rec, loop_rec, rm_done = run_prepare(STOCK_POOL)
check(ret is True and len(ensure_rec.calls) == 1,
      "A2 stock pool: ensure_binder_devices called once")
# A3: listdir failure -> fail-open into ensure.
ret, ensure_rec, loop_rec, rm_done = run_prepare(
    FULL_POOL, listdir_exc=OSError("no binderfs"))
check(ret is True and len(ensure_rec.calls) == 1,
      "A3 listdir OSError: falls through to ensure_binder_devices")
# A4: empty stub dir with full names -> rmdir attempted, ensure called.
ret, ensure_rec, loop_rec, rm_done = run_prepare(
    FULL_POOL, isdir_names=("binder45",))
check(len(ensure_rec.calls) == 1 and rm_done == ["binder45"],
      "A4 stub dir detected: rmdir attempted + ensure called",
      repr(rm_done))
# A5: non-empty stub (rmdir raises) -> left alone, no raise, ensure called.
ret, ensure_rec, loop_rec, rm_done = run_prepare(
    FULL_POOL, isdir_names=("binder7",), rmdir_fail=("binder7",))
check(ret is True and len(ensure_rec.calls) == 1 and rm_done == [],
      "A5 non-empty stub: rmdir failure tolerated, ensure still called")
# A6: everything raising -> fail-open True, no exception escapes.
ret, ensure_rec, loop_rec, rm_done = run_prepare(
    FULL_POOL, ensure_exc=RuntimeError("boom"), count_exc=RuntimeError("x"),
    listdir_exc=OSError("gone"))
check(ret is True, "A6 all helpers raising: fail-open True, never raises")
# A7: loop pool below floor -> ensure_loop_devices called.
ret, ensure_rec, loop_rec, rm_done = run_prepare(FULL_POOL, count_loops=8)
check(len(loop_rec.calls) == 1 and not ensure_rec.calls,
      "A7 loop count 8 < floor: ensure_loop_devices called, binder untouched")
# A8: loop pool healthy -> not called.
ret, ensure_rec, loop_rec, rm_done = run_prepare(FULL_POOL, count_loops=61)
check(not loop_rec.calls, "A8 loop count 61 >= floor: no loop ensure")
# A9: count_loop_devices raising -> tolerated, still True.
ret, ensure_rec, loop_rec, rm_done = run_prepare(
    FULL_POOL, count_exc=ValueError("bad"))
check(ret is True and not loop_rec.calls,
      "A9 count_loop_devices raising: tolerated, returns True")

print("== B. instance_single_action wiring ==")
CALLS = []
INSTANCES = {"rbx1": {"provider": "local"}}
PREP_BEHAVIOR = {"raise": False}


def fake_prepare():
    if PREP_BEHAVIOR["raise"]:
        raise RuntimeError("prepare exploded")
    CALLS.append("prepare")
    return True


def fake_run2(cmd, timeout=30):
    CALLS.append(cmd)
    return 0, "", ""


def reset_b():
    CALLS.clear()
    PREP_BEHAVIOR["raise"] = False
    app.load_instances = lambda: dict(INSTANCES)
    app.load_providers = lambda: {}
    app.run = fake_run2
    app.container_status = lambda c: "running"
    app.invalidate_instance_snapshot = lambda n: None
    app._prepare_pools_for_start = fake_prepare
    app.check_roblox = lambda n: True


reset_b()
code, _ = app.instance_single_action("rbx1", "start")
check(code == 200 and CALLS == ["prepare", "docker start wd-rbx1"],
      "B1 start: prepare runs BEFORE docker start",
      repr(CALLS))
reset_b()
PREP_BEHAVIOR["raise"] = True
code, _ = app.instance_single_action("rbx1", "start")
check(code == 200 and CALLS == ["docker start wd-rbx1"],
      "B2 prepare raising: docker start still attempted (fail-open)",
      repr(CALLS))
reset_b()
code, _ = app.instance_single_action("rbx1", "restart")
check(code == 200 and CALLS == ["docker stop wd-rbx1", "prepare",
                               "docker start wd-rbx1"],
      "B3 restart: stop -> prepare -> start, in order",
      repr(CALLS))
reset_b()
code, _ = app.instance_single_action("rbx1", "stop")
check(code == 200 and CALLS == ["docker stop wd-rbx1"],
      "B4 stop: prepare NOT called",
      repr(CALLS))
reset_b()
code, _ = app.instance_single_action("nope", "start")
check(code == 404 and CALLS == [],
      "B5 unknown instance: 404, prepare NOT called",
      repr(CALLS))

print("== C. wing agent 1.0.33 ==")
check(agent.VERSION in ("1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"), "C1 agent VERSION is 1.0.35+",
      repr(agent.VERSION))
check(agent.parse_agent_version("1.0.35") > agent.parse_agent_version("1.0.34"),
      "C2 parse_agent_version orders 1.0.35 > 1.0.34")


def run_agent_prepare(dirnames, isdir_names=(), listdir_exc=None,
                     count_loops=64, raise_all=False):
    pool_rec, loop_rec = Rec(), Rec()

    def fake_pool(*a, **k):
        pool_rec()
        if raise_all:
            raise RuntimeError("pool boom")
        return (64, False)

    def fake_count(*a, **k):
        if raise_all:
            raise ValueError("count boom")
        return count_loops

    def fake_listdir(p):
        if listdir_exc:
            raise listdir_exc
        return list(dirnames)

    def fake_isdir(p):
        return os.path.basename(p) in isdir_names

    old = (agent.sw_boot_binder_pool, agent.sw_count_loop_devices,
           agent.sw_ensure_loop_devices)
    agent.sw_boot_binder_pool = fake_pool
    agent.sw_count_loop_devices = fake_count
    agent.sw_ensure_loop_devices = lambda *a, **k: loop_rec()
    try:
        with mock.patch.object(os, "listdir", fake_listdir), \
             mock.patch.object(os.path, "isdir", fake_isdir):
            agent._sw_prepare_pools_for_start()
    finally:
        (agent.sw_boot_binder_pool, agent.sw_count_loop_devices,
         agent.sw_ensure_loop_devices) = old
    return pool_rec, loop_rec


# C3: complete pool -> no restore.
pool_rec, loop_rec = run_agent_prepare(FULL_POOL)
check(not pool_rec.calls and not loop_rec.calls,
      "C3 agent complete pool: no binder restore, no loop ensure")
# C4: stock pool -> restore called once.
pool_rec, loop_rec = run_agent_prepare(STOCK_POOL)
check(len(pool_rec.calls) == 1,
      "C4 agent stock pool: sw_boot_binder_pool called once")
# C5: stub dir with all names present -> still triggers restore.
pool_rec, loop_rec = run_agent_prepare(FULL_POOL, isdir_names=("binder12",))
check(len(pool_rec.calls) == 1,
      "C5 agent stub dir: restore triggered despite full name set")
# C6: listdir failure -> fail-open into restore.
pool_rec, loop_rec = run_agent_prepare(FULL_POOL, listdir_exc=OSError("x"))
check(len(pool_rec.calls) == 1,
      "C6 agent listdir OSError: falls through to restore")
# C7: everything raising -> no exception escapes.
try:
    run_agent_prepare(FULL_POOL, raise_all=True, listdir_exc=OSError("y"))
    check(True, "C7 agent all helpers raising: never raises")
except Exception as e:
    bad("C7 agent all helpers raising: never raises", repr(e))
# C8: loop floor wiring.
pool_rec, loop_rec = run_agent_prepare(FULL_POOL, count_loops=8)
check(len(loop_rec.calls) == 1 and not pool_rec.calls,
      "C8 agent loop 8 < floor: loop ensure called, binder untouched")
pool_rec, loop_rec = run_agent_prepare(FULL_POOL, count_loops=60)
check(not loop_rec.calls,
      "C9 agent loop 60 >= floor: no loop ensure")

print("== D. /start endpoint ordering contract ==")
src = inspect.getsource(agent.Handler.do_POST)
i_route = src.find('/api/agent/instances/([^/]+)/start')
check(i_route != -1, "D1 /start route present in do_POST")
if i_route != -1:
    # Bound the block at the next route registration.
    i_next = src.find('m = re.fullmatch(r"/api/agent/instances/', i_route + 1)
    block = src[i_route:i_next if i_next != -1 else i_route + 2000]
    i_prep = block.find("_sw_prepare_pools_for_start()")
    i_docker = block.find('docker("start"')
    check(i_prep != -1 and i_docker != -1 and i_prep < i_docker,
          "D2 /start: pool prepare runs BEFORE docker(\"start\"",
          f"prep@{i_prep} docker@{i_docker}")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
