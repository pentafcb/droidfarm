#!/usr/bin/env python3
"""Tests for the per-instance docker snapshot cache (10s TTL).

Covers:
  A. cache hit/miss/TTL semantics (hermetic, fake docker_inspect)
  B. fresh snapshot content: status/ip/setup/health/contract (fake DF_ST)
  C. never-raises behavior on garbage inputs and failing sources
  D. invalidation semantics
  E. threadsafety smoke test
  F. route/source wiring contract (GET branch uses the cache, mutations invalidate)
"""
import importlib.util
import os
import sys
import tempfile
import threading
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")

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


def load_app():
    spec = importlib.util.spec_from_file_location("df_app_snap", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RUNNING = {"State": {"Running": True},
           "NetworkSettings": {"Networks": {"droid-net": {"IPAddress": "10.20.0.11"}}}}
STOPPED = {"State": {"Running": False},
           "NetworkSettings": {"Networks": {"droid-net": {"IPAddress": "10.20.0.12"}}}}


class FakeDocker:
    """Controllable docker_inspect replacement with a call log."""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, cname):
        with self.lock:
            self.calls.append(cname)
        if self.exc:
            raise self.exc
        return self.result


print("== A. cache hit/miss/TTL ==")
with tempfile.TemporaryDirectory() as st:
    os.environ["DF_ST"] = st
    app = load_app()
    fake = FakeDocker(result=RUNNING)
    app.docker_inspect = fake
    app._snapshot_cache.clear()

    s1 = app.instance_snapshot("rbx1", {"ip": "10.20.0.11"})
    check("fresh snapshot calls docker_inspect once", len(fake.calls) == 1,
          repr(fake.calls))
    check("status from inspect", s1["status"] == "running", repr(s1))
    check("ip prefers instances.json over inspect",
          s1["ip"] == "10.20.0.11", repr(s1["ip"]))

    s2 = app.instance_snapshot("rbx1", {"ip": "10.20.0.11"})
    check("second call within TTL: no new inspect", len(fake.calls) == 1,
          repr(len(fake.calls)))
    check("cached data identical", s2 == s1, repr(s2))

    s2["status"] = "MUTATED"
    s3 = app.instance_snapshot("rbx1", {"ip": "10.20.0.11"})
    check("returned dict is a copy (mutation does not poison cache)",
          s3["status"] == "running", repr(s3))

    # force TTL expiry by aging the entry
    with app._snapshot_lock:
        e = app._snapshot_cache["rbx1"]
        e = {"ts": e["ts"] - app._SNAPSHOT_TTL - 1, "data": e["data"]}
        app._snapshot_cache["rbx1"] = e
    fake2 = FakeDocker(result=STOPPED)
    app.docker_inspect = fake2
    s4 = app.instance_snapshot("rbx1", {})
    check("expired TTL -> fresh inspect", len(fake2.calls) == 1, repr(len(fake2.calls)))
    check("expired TTL -> new data used", s4["status"] == "stopped", repr(s4))

    # per-instance isolation
    fake = FakeDocker(result=RUNNING)
    app.docker_inspect = fake
    app._snapshot_cache.clear()
    app.instance_snapshot("rbx1", {})
    app.instance_snapshot("rbx2", {})
    check("instances cached separately", set(app._snapshot_cache) == {"rbx1", "rbx2"},
          repr(set(app._snapshot_cache)))
    check("two inspects for two instances", len(fake.calls) == 2, repr(len(fake.calls)))

print("== B. snapshot content ==")
with tempfile.TemporaryDirectory() as st:
    os.environ["DF_ST"] = st
    app = load_app()
    app._snapshot_cache.clear()

    with open(os.path.join(st, "rbx1.degraded"), "w") as f:
        f.write("container restarted 7 times - auto-recovery paused\n")
    with open(os.path.join(st, "rbx1.roblox_drift"), "w") as f:
        f.write("installed 2.999.1, pinned 2.740.931\n")
    fake = FakeDocker(result=RUNNING)
    app.docker_inspect = fake
    with mock.patch("os.path.exists", return_value=False):
        s = app.instance_snapshot("rbx1", {})
    check("degraded marker surfaced", s["degraded"] is True and
          s["degraded_reason"] == "container restarted 7 times - auto-recovery paused",
          repr(s))
    check("drift marker surfaced", s["roblox_drift"] is True and
          s["roblox_drift_reason"] == "installed 2.999.1, pinned 2.740.931",
          repr(s))
    check("running, no markers -> booting", s["setup"] == "booting", repr(s))

    def exists_new(path):
        return path.endswith("/booted") or path.endswith("/roblox_done")

    app._snapshot_cache.clear()
    with mock.patch("os.path.exists", side_effect=exists_new):
        s = app.instance_snapshot("rbx1", {})
    check("running + booted + roblox_done -> ready", s["setup"] == "ready", repr(s))

    def exists_booted(path):
        return path.endswith("/booted")

    app._snapshot_cache.clear()
    with mock.patch("os.path.exists", side_effect=exists_booted):
        s = app.instance_snapshot("rbx1", {})
    check("running + booted only -> installing_roblox",
          s["setup"] == "installing_roblox", repr(s))

    app._snapshot_cache.clear()
    app.docker_inspect = FakeDocker(result=STOPPED)
    with mock.patch("os.path.exists", side_effect=exists_new):
        s = app.instance_snapshot("rbx9", {})
    check("stopped -> setup stopped (markers ignored)", s["setup"] == "stopped",
          repr(s))

    app._snapshot_cache.clear()
    app.docker_inspect = FakeDocker(result=RUNNING)
    with mock.patch("os.path.exists", return_value=False):
        s = app.instance_snapshot("rbx9", {"ip": ""})
    check("empty inst ip -> falls back to inspect IP",
          s["ip"] == "10.20.0.11", repr(s["ip"]))

    app._snapshot_cache.clear()
    app.docker_inspect = FakeDocker(result=None)
    with mock.patch("os.path.exists", return_value=False):
        s = app.instance_snapshot("rbx9", {})
    check("inspect None -> missing/missing/None",
          s["status"] == "missing" and s["setup"] == "missing" and s["ip"] is None,
          repr(s))

    expected_keys = {"status", "ip", "setup", "degraded", "degraded_reason",
                     "roblox_drift", "roblox_drift_reason"}
    check("snapshot key contract", set(s) == expected_keys, repr(set(s)))

print("== C. never-raises ==")
with tempfile.TemporaryDirectory() as st:
    os.environ["DF_ST"] = st
    app = load_app()
    app._snapshot_cache.clear()
    app.docker_inspect = FakeDocker(result=RUNNING, exc=RuntimeError("boom"))
    try:
        s = app.instance_snapshot("rbx1", {"ip": "1.2.3.4"})
        check("docker_inspect raising -> safe defaults, no raise",
              s["status"] == "missing" and s["setup"] == "missing", repr(s))
    except Exception as e:  # noqa: BLE001
        bad("docker_inspect raising -> safe defaults, no raise", repr(e))

    app._snapshot_cache.clear()
    app.docker_inspect = FakeDocker(result=["not", "a", "dict"])
    try:
        s = app.instance_snapshot("rbx1", {})
        check("garbage inspect result -> safe defaults, no raise",
              s["status"] == "missing", repr(s))
    except Exception as e:  # noqa: BLE001
        bad("garbage inspect result -> safe defaults, no raise", repr(e))

    for bad_in in (None, 123, "../..", "", "rbx1; rm -rf /"):
        try:
            s = app.instance_snapshot(bad_in, None)
            ok2 = isinstance(s, dict) and set(s) == expected_keys
        except Exception:  # noqa: BLE001
            ok2 = False
        check(f"garbage name/inst never raises ({bad_in!r})", ok2)

    app._snapshot_cache.clear()
    app.docker_inspect = FakeDocker(result=RUNNING)
    with mock.patch("os.path.exists", side_effect=RuntimeError("fs boom")):
        try:
            s = app.instance_snapshot("rbx1", {})
            check("exists() raising -> still safe, no raise",
                  s["status"] == "missing" and s["setup"] == "missing", repr(s))
        except Exception as e:  # noqa: BLE001
            bad("exists() raising -> still safe, no raise", repr(e))

print("== D. invalidation ==")
with tempfile.TemporaryDirectory() as st:
    os.environ["DF_ST"] = st
    app = load_app()
    app._snapshot_cache.clear()
    fake = FakeDocker(result=RUNNING)
    app.docker_inspect = fake
    app.instance_snapshot("rbx1", {})
    check("populated", "rbx1" in app._snapshot_cache)
    app.invalidate_instance_snapshot("rbx1")
    check("invalidate drops the entry", "rbx1" not in app._snapshot_cache)
    try:
        app.invalidate_instance_snapshot("nope")
        app.invalidate_instance_snapshot(None)
        app.invalidate_instance_snapshot("../evil")
        check("invalidate on unknown/garbage never raises", True)
    except Exception as e:  # noqa: BLE001
        bad("invalidate on unknown/garbage never raises", repr(e))
    app.instance_snapshot("../rbx1", {})
    check("unsafe name sanitized for docker_inspect",
          fake.calls[-1] == "wd-rbx1", repr(fake.calls[-1]))
    check("unsafe name cached under safe key", "rbx1" in app._snapshot_cache)

print("== E. threadsafety ==")
with tempfile.TemporaryDirectory() as st:
    os.environ["DF_ST"] = st
    app = load_app()
    app._snapshot_cache.clear()
    app.docker_inspect = FakeDocker(result=RUNNING)
    errors = []

    def hammer():
        try:
            for _ in range(50):
                s = app.instance_snapshot("rbx1", {})
                assert s["status"] == "running"
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=hammer) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    check("10 threads x 50 calls: no exceptions", not errors, repr(errors[:3]))

print("== F. wiring contract ==")
src = open(APP).read()
check("GET /api/instances local branch uses instance_snapshot",
      "snap = instance_snapshot(name, inst)" in src)
check("old per-poll container_status/container_ip assembly gone",
      "status = container_status(cname)" not in src)
n_inv = src.count("invalidate_instance_snapshot(name)")
check("mutations invalidate (single_action x4 + delete x1)",
      n_inv >= 5, f"found {n_inv}")
check("single_action start/stop path invalidates",
      'action in ("start", "stop")' in src and n_inv >= 5)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
