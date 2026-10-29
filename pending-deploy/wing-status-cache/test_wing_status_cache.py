#!/usr/bin/env python3
"""Tests for wing agent 1.0.20 per-instance status snapshot cache (manager
instance-health-cache parity).

The manager polls /api/agent/instances every 10s per provider. Each poll ran
`docker ps -a` + `docker inspect -f {{.State.StartedAt}}` once per instance
— 2 serialized subprocess spawns per instance, on every poll, from every
connected dashboard client. The manager fixed the identical hot path on
2026-10-10 (instance-health-cache); the agent never did. This suite covers:

  A. agent.VERSION is 1.0.20+ (1.0.21 adds headroom probe) + manager gate
  B. constants: _AGENT_STATUS_TTL == 10.0, key regex shape
  C. cache key units (valid / invalid / hostile names, never raises)
  D. miss -> live sources consulted (docker ps + inspect counted)
  E. hit -> zero docker calls within TTL; fresh dict each call (mutation
     isolation)
  F. TTL expiry -> refetch; per-instance isolation
  G. snapshot content: running/ready, booting, stopped, started_at parse,
     mem/cpu/drift passthrough
  H. invalid name -> uncached passthrough (today's behavior), no cache write
  I. invalidate_agent_status_cache: drops entry, unknown name no-op,
     never raises
  J. raising docker -> fallback dict, never raises; garbage never raises
  K. stale-entry prune (>60s dropped on next write)
  L. thread-safety smoke: 10 threads x 30 calls, bounded docker calls
  M. route wiring: do_GET /instances + /status/{name} use the cached
     wrapper; create/start/stop/delete routes invalidate
"""
import importlib.util
import inspect
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
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


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("wing_agent_status_cache", AGENT)
app = load("manager_app_status_cache", APP)

# ---------------------------------------------------------------- fake env

REAL_TIME = agent.time


class FakeTime:
    def __init__(self):
        self.now = 1000000.0

    def time(self):
        return self.now


class FakeDocker:
    """Counts calls; models container states + StartedAt."""

    def __init__(self):
        self.calls = []
        self.containers = {}   # cname -> "Up ..." / "Exited ..." / absent
        self.started_at = {}   # cname -> iso string
        self.raise_on = None

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if self.raise_on is not None and self.raise_on in args:
            raise RuntimeError("boom")
        if args[0] == "ps":
            lines = [f"{c}\t{s}" for c, s in self.containers.items()]
            return 0, "\n".join(lines)
        if args[0] == "inspect":
            cname = args[-1]
            iso = self.started_at.get(cname)
            if iso is None:
                return 1, "Error: No such object"
            return 0, iso
        return 0, ""


def make_env():
    """Fresh fake docker + patched helpers; returns (dock, restore)."""
    dock = FakeDocker()
    old_docker = agent.docker
    old_res = agent.instance_resources_all
    old_drift = agent.read_roblox_drift
    old_time = agent.time
    ft = FakeTime()
    agent.docker = dock
    agent.time = ft
    agent.instance_resources_all = lambda: {"srt1": {"mem_used_mb": 512.0,
                                                     "cpu_pct": 12.5}}
    agent.read_roblox_drift = lambda n: (False, None)
    agent._agent_status_cache.clear()
    real_exists = os.path.exists

    def fake_exists(p):
        p = str(p)
        if p.endswith("/booted"):
            return "booted" in fake_exists.markers
        if p.endswith("/roblox_done"):
            return "done" in fake_exists.markers
        return real_exists(p)

    fake_exists.markers = set()
    os.path.exists = fake_exists

    def restore():
        agent.docker = old_docker
        agent.instance_resources_all = old_res
        agent.read_roblox_drift = old_drift
        agent.time = old_time
        os.path.exists = real_exists
        agent._agent_status_cache.clear()

    return dock, ft, fake_exists, restore


def ps_calls(dock):
    return [c for c in dock.calls if c[0] == "ps"]


def inspect_calls(dock):
    return [c for c in dock.calls if c[0] == "inspect"]


# ---------------------------------------------------------------- A: version

check_v = agent.VERSION in ("1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45")
if check_v:
    ok("A1 VERSION is 1.0.20+ (1.0.21 adds headroom probe, 1.0.22 adds per-instance degraded)")
else:
    bad("A1 VERSION is 1.0.20+ (1.0.21 adds headroom probe, 1.0.22 adds per-instance degraded)", repr(agent.VERSION))

try:
    gate = app._version_at_least("1.0.20", "1.0.2")
except Exception as e:
    gate = "RAISED: %r" % e
if gate is True:
    ok("A2 manager >=1.0.2 gate accepts 1.0.20")
else:
    bad("A2 manager >=1.0.2 gate accepts 1.0.20", repr(gate))

# ---------------------------------------------------------------- B: constants

if agent._AGENT_STATUS_TTL == 10.0:
    ok("B1 _AGENT_STATUS_TTL == 10.0 (dashboard-cadence parity)")
else:
    bad("B1 _AGENT_STATUS_TTL == 10.0", repr(agent._AGENT_STATUS_TTL))

if re.fullmatch(agent._AGENT_STATUS_KEY_RE, "srt1-2_x") and \
        not re.fullmatch(agent._AGENT_STATUS_KEY_RE, "../x") and \
        not re.fullmatch(agent._AGENT_STATUS_KEY_RE, "a" * 33):
    ok("B2 key regex: plain ids in, traversal/oversize out")
else:
    bad("B2 key regex shape", repr(agent._AGENT_STATUS_KEY_RE))

# ---------------------------------------------------------------- C: key units

cases = [("srt1", "srt1"), ("rbx_20-x", "rbx_20-x"), ("a" * 32, "a" * 32),
         ("../x", None), ("", None), ("a b", None), ("a" * 33, None),
         (None, None), ("wd-srt1;", None), ("srt1\n", None)]
key_ok = True
for name, want in cases:
    try:
        got = agent._agent_status_cache_key(name)
    except Exception as e:
        got, key_ok = "RAISED", False
        bad("C key never raises", "%r on %r" % (e, name))
    if got != want:
        key_ok = False
        bad("C key(%r)" % (name,), "got %r want %r" % (got, want))
if key_ok:
    ok("C key units (valid/invalid/hostile)")

# ---------------------------------------------------------------- D/E/F: miss/hit/TTL/isolation

dock, ft, fake_exists, restore = make_env()
try:
    dock.containers["wd-srt1"] = "Up 3 hours"
    dock.started_at["wd-srt1"] = "2026-10-18T12:00:00Z"

    d1 = agent.cached_instance_status_linux("srt1")
    n_ps, n_in = len(ps_calls(dock)), len(inspect_calls(dock))
    if n_ps == 1 and n_in == 1:
        ok("D miss consults live sources exactly once (ps + inspect)")
    else:
        bad("D miss live-source count", f"ps={n_ps} inspect={n_in}")

    d2 = agent.cached_instance_status_linux("srt1")
    if len(ps_calls(dock)) == 1 and len(inspect_calls(dock)) == 1:
        ok("E hit within TTL: zero new docker calls")
    else:
        bad("E hit made docker calls", f"ps={len(ps_calls(dock))}")

    if d1 is not d2 and d1 == d2:
        ok("E fresh dict each call (equal, not identical)")
    else:
        bad("E fresh-dict contract", "identical object" if d1 is d2 else "unequal")

    d1["setup"] = "MUTATED"
    d3 = agent.cached_instance_status_linux("srt1")
    if d3["setup"] != "MUTATED":
        ok("E caller mutation cannot poison the cache")
    else:
        bad("E cache poisoned by caller mutation")

    ft.now += 11.0
    agent.cached_instance_status_linux("srt1")
    if len(ps_calls(dock)) == 2 and len(inspect_calls(dock)) == 2:
        ok("F TTL expiry refetches")
    else:
        bad("F expiry did not refetch", f"ps={len(ps_calls(dock))}")

    dock.containers["wd-srt2"] = "Up 1 hour"
    dock.started_at["wd-srt2"] = "2026-10-18T13:00:00Z"
    n_before = len(dock.calls)
    agent.cached_instance_status_linux("srt2")
    # srt1 was just refreshed at ft.now; a srt1 hit must not refetch
    agent.cached_instance_status_linux("srt1")
    if len(dock.calls) == n_before + 2:
        ok("F per-instance isolation (one instance's miss != another's)")
    else:
        bad("F isolation", f"delta={len(dock.calls) - n_before}")
finally:
    restore()

# ---------------------------------------------------------------- G: snapshot content

dock, ft, fake_exists, restore = make_env()
try:
    dock.containers["wd-srt1"] = "Up 3 hours"
    dock.started_at["wd-srt1"] = "2026-10-18T12:00:00Z"
    fake_exists.markers = {"booted", "done"}
    d = agent.cached_instance_status_linux("srt1")
    g_ok = (d["running"] is True and d["setup"] == "ready"
            and d["started_at"] > 1700000000
            and d["mem_used_mb"] == 512.0 and d["cpu_pct"] == 12.5
            and d["roblox_drift"] is False)
    if g_ok:
        ok("G ready snapshot: running/setup/started_at/mem/cpu/drift")
    else:
        bad("G ready snapshot", repr(d))

    agent.invalidate_agent_status_cache("srt1")
    fake_exists.markers = set()
    d = agent.cached_instance_status_linux("srt1")
    if d["setup"] == "booting" and d["roblox"] is False:
        ok("G booting snapshot (no markers)")
    else:
        bad("G booting snapshot", repr(d))

    dock.containers["wd-srt1"] = "Exited (0) 2 hours ago"
    agent.invalidate_agent_status_cache("srt1")
    d = agent.cached_instance_status_linux("srt1")
    if d["running"] is False and d["setup"] == "stopped":
        ok("G stopped snapshot")
    else:
        bad("G stopped snapshot", repr(d))

    # drift passthrough
    old_drift = agent.read_roblox_drift
    agent.read_roblox_drift = lambda n: (True, "2.999.1 != 2.740.931")
    dock.containers["wd-srt1"] = "Up 1 hour"
    agent.invalidate_agent_status_cache("srt1")
    d = agent.cached_instance_status_linux("srt1")
    agent.read_roblox_drift = old_drift
    if d["roblox_drift"] is True and "2.999.1" in (d["roblox_drift_reason"] or ""):
        ok("G drift fields passthrough")
    else:
        bad("G drift fields", repr(d))
finally:
    restore()

# ---------------------------------------------------------------- H: invalid names bypass
# (t170 agent-status-delete-sink-gate changed this section's contract:
# invalid names still bypass the cache and are never cached, but the
# status SINK now fails closed — stopped dict with zero docker calls —
# instead of calling through to container_status/docker inspect. The
# old "uncached passthrough" assertion encoded the pre-fix behavior.)

dock, ft, fake_exists, restore = make_env()
try:
    dock.containers["wd-None"] = "Up 1 hour"
    n0 = len(dock.calls)
    r1 = agent.cached_instance_status_linux("../x")
    r2 = agent.cached_instance_status_linux("../x")
    if (len(dock.calls) == n0 and isinstance(r1, dict) and
            isinstance(r2, dict) and r1.get("running") is False and
            r1.get("setup") == "stopped" and r1 == r2):
        ok("H invalid name: bypasses cache, sink fails closed (stopped, zero docker calls)")
    else:
        bad("H invalid-name closed sink", f"calls delta={len(dock.calls) - n0} r1={r1!r}")
    if "../x" not in agent._agent_status_cache:
        ok("H invalid name never cached")
    else:
        bad("H invalid name cached", "")
finally:
    restore()

# ---------------------------------------------------------------- I: invalidation

dock, ft, fake_exists, restore = make_env()
try:
    dock.containers["wd-srt1"] = "Up 1 hour"
    agent.cached_instance_status_linux("srt1")
    n0 = len(dock.calls)
    agent.invalidate_agent_status_cache("srt1")
    agent.cached_instance_status_linux("srt1")
    if len(dock.calls) > n0:
        ok("I invalidate drops the entry (next call refetches)")
    else:
        bad("I invalidate did not drop", "")
    try:
        agent.invalidate_agent_status_cache("nope")
        agent.invalidate_agent_status_cache(None)
        agent.invalidate_agent_status_cache("../x")
        ok("I unknown/invalid invalidate: no-op, never raises")
    except Exception as e:
        bad("I invalidate never raises", repr(e))
finally:
    restore()

# ---------------------------------------------------------------- J: never raises

dock, ft, fake_exists, restore = make_env()
try:
    dock.raise_on = "ps"
    try:
        r = agent.cached_instance_status_linux("srt1")
        if isinstance(r, dict):
            ok("J raising docker -> fallback dict, never raises")
        else:
            bad("J raising docker shape", repr(r))
    except Exception as e:
        bad("J raising docker", repr(e))
    for g in (None, 123, ["x"], {"a": 1}, "srt1; rm -rf /"):
        try:
            agent.cached_instance_status_linux(g)
            agent.invalidate_agent_status_cache(g)
        except Exception as e:
            bad("J garbage never raises", "%r -> %r" % (g, e))
            break
    else:
        ok("J garbage names never raise")
finally:
    restore()

# ---------------------------------------------------------------- K: prune

dock, ft, fake_exists, restore = make_env()
try:
    dock.containers["wd-srt1"] = "Up 1 hour"
    dock.containers["wd-srt2"] = "Up 1 hour"
    agent.cached_instance_status_linux("srt1")  # t=1000000
    ft.now += 61.0
    agent.cached_instance_status_linux("srt2")  # write prunes srt1
    if "srt1" not in agent._agent_status_cache and "srt2" in agent._agent_status_cache:
        ok("K stale entries (>60s) pruned on write")
    else:
        bad("K prune", repr(sorted(agent._agent_status_cache)))
finally:
    restore()

# ---------------------------------------------------------------- L: thread smoke

dock, ft, fake_exists, restore = make_env()
try:
    dock.containers["wd-srt1"] = "Up 1 hour"
    errors = []

    def worker():
        try:
            for _ in range(30):
                agent.cached_instance_status_linux("srt1")
        except Exception as e:
            errors.append(e)

    ts = [threading.Thread(target=worker) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    # 300 calls, 10s TTL frozen: first fill costs 2 docker calls; racers may
    # add a few more; anything over 40 means the lock/double-fill is broken.
    if not errors and len(dock.calls) <= 40:
        ok("L 10 threads x 30 calls: no errors, bounded docker calls "
           f"({len(dock.calls)})")
    else:
        bad("L thread smoke", f"errors={errors} calls={len(dock.calls)}")
finally:
    restore()

# ---------------------------------------------------------------- M: route wiring

get_src = inspect.getsource(agent.Handler.do_GET)
post_src = inspect.getsource(agent.Handler.do_POST)
del_src = inspect.getsource(agent.Handler.do_DELETE)

bare_in_get = re.findall(r"(?<!cached_)instance_status_linux\(", get_src)
if not bare_in_get and "cached_instance_status_linux(name)" in get_src:
    ok("M do_GET /api/agent/instances uses the cached wrapper (no bare calls)")
else:
    bad("M do_GET /instances wiring", f"bare={bare_in_get}")

if "cached_instance_status_linux(name)" in get_src:
    ok("M do_GET /status/{name} uses the cached wrapper")
else:
    bad("M do_GET /status wiring", "")

if "create_instance_linux(name, cpus, memory_mb, ip)" in post_src and \
        "invalidate_agent_status_cache(name)" in post_src:
    ok("M create route invalidates")
else:
    bad("M create invalidation", "")

if post_src.count("invalidate_agent_status_cache(name)") >= 3:
    ok("M start + stop + create routes all invalidate")
else:
    bad("M start/stop invalidation count",
        str(post_src.count("invalidate_agent_status_cache(name)")))

if "invalidate_agent_status_cache(name)" in del_src:
    ok("M delete route invalidates")
else:
    bad("M delete invalidation", "")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
