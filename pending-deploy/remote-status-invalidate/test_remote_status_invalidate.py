"""Unit tests for optimistic remote-status patching (manager/app.py).

Run: python3 test_remote_status_invalidate.py
Covers: patch_remote_status() / drop_remote_status() and their wiring.

The gap: fetch_remote_statuses() serves the last-known-good snapshot when
the fail-fast live fetch fails, but the cache was never touched by
manager-initiated remote mutations. A flaky poll right after a dashboard
Stop showed the pre-action state ("running") for up to _REMOTE_STATUS_TTL
seconds; after a Start it showed "stopped". The manager now patches the
instance's `running` flag optimistically on successful remote
start/stop/restart/create, and prunes the entry on remote delete.

Hermetic: imports the real manager/app.py; no docker, no network,
no filesystem writes (wing_request/load_providers are faked in-memory).
"""
import importlib.util
import inspect
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

app = load("rsinv_app", os.path.join(HERE, "..", "..", "manager", "app.py"))

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")

def reset_cache():
    app._remote_status_cache.clear()

def get_entry(pid, name):
    cur = app._remote_status_cache.get(pid)
    if cur and isinstance(cur[1], dict):
        return cur[1].get((pid, name))
    return None

# ---------------- patch_remote_status: helper units ----------------
reset_cache()
app.patch_remote_status("p1", "n1", True)
e = get_entry("p1", "n1")
check("patch: empty cache creates minimal entry",
      e == {"name": "n1", "running": True})

reset_cache()
app._remote_status_cache["p1"] = (time.monotonic(), {
    ("p1", "n1"): {"name": "n1", "running": True, "setup": "ready",
                   "degraded": False, "mem_used_mb": 512.0},
    ("p1", "n2"): {"name": "n2", "running": True, "setup": "ready"},
})
app.patch_remote_status("p1", "n1", False)
e1, e2 = get_entry("p1", "n1"), get_entry("p1", "n2")
check("patch: flips running only, other fields preserved",
      e1["running"] is False and e1["setup"] == "ready"
      and e1["degraded"] is False and e1["mem_used_mb"] == 512.0)
check("patch: other instances untouched",
      e2 == {"name": "n2", "running": True, "setup": "ready"})

reset_cache()
app._remote_status_cache["pa"] = (time.monotonic(), {("pa", "na"): {"name": "na", "running": False}})
app._remote_status_cache["pb"] = (time.monotonic(), {("pb", "na"): {"name": "na", "running": False}})
app.patch_remote_status("pa", "na", True)
check("patch: other providers untouched",
      get_entry("pa", "na")["running"] is True
      and get_entry("pb", "na")["running"] is False)

reset_cache()
app.patch_remote_status("p1", "n1", False)
e = get_entry("p1", "n1")
check("patch: False on missing entry", e == {"name": "n1", "running": False})

reset_cache()
app.patch_remote_status("p1", "n1", "yes")  # truthy non-bool coerced
check("patch: running coerced to strict bool",
      get_entry("p1", "n1")["running"] is True)

reset_cache()
app.patch_remote_status("p1", "n1", 0)
check("patch: falsy coerced to False",
      get_entry("p1", "n1")["running"] is False)

# never-raises on garbage
for gpid, gname, grun in [(None, None, True), (123, 456, False),
                          (object(), object(), True),
                          ("p", "n", object())]:
    try:
        app.patch_remote_status(gpid, gname, grun)
        ok = True
    except Exception:
        ok = False
    check(f"patch: never raises on garbage ({gpid!r})", ok)
reset_cache()

# key parity: the entry lands under exactly the (pid, name) tuple the
# /api/instances remote branch looks up via fetch_remote_statuses
reset_cache()
app.patch_remote_status("prov-x", "rbx9", True)
cur = app._remote_status_cache.get("prov-x")
check("patch: key is the exact (pid, name) tuple",
      isinstance(cur, tuple) and ("prov-x", "rbx9") in cur[1]
      and cur[1][("prov-x", "rbx9")]["running"] is True)

# a fresh fetch overwrites the optimistic patch (it can never stick)
reset_cache()
app.patch_remote_status("p1", "n1", True)
app._remote_status_cache["p1"] = (time.monotonic(), {
    ("p1", "n1"): {"name": "n1", "running": False, "setup": "ready"}})
check("patch: replaced wholesale by newer snapshot",
      get_entry("p1", "n1") == {"name": "n1", "running": False, "setup": "ready"})
reset_cache()

# ---------------- drop_remote_status: helper units ----------------
app._remote_status_cache["p1"] = (111.0, {
    ("p1", "n1"): {"name": "n1", "running": True},
    ("p1", "n2"): {"name": "n2", "running": False},
})
app.drop_remote_status("p1", "n1")
cur = app._remote_status_cache["p1"]
check("drop: entry removed, timestamp kept, others kept",
      cur[0] == 111.0 and ("p1", "n1") not in cur[1]
      and cur[1][("p1", "n2")]["running"] is False)

app.drop_remote_status("p1", "missing")
check("drop: missing name is a silent no-op",
      ("p1", "n2") in app._remote_status_cache["p1"][1])
app.drop_remote_status("no-such-provider", "n1")
check("drop: missing provider is a silent no-op", True)

for gpid, gname in [(None, None), (123, 456), (object(), object())]:
    try:
        app.drop_remote_status(gpid, gname)
        ok = True
    except Exception:
        ok = False
    check(f"drop: never raises on garbage ({gpid!r})", ok)
reset_cache()

# ---------------- wiring contracts (source) ----------------
src_single = inspect.getsource(app.instance_single_action)
check("wire: restart path patches running=True on 200",
      "patch_remote_status(prov, name, True)" in src_single)
check("wire: start/stop patch guarded by code == 200",
      'if code == 200 and action in ("start", "stop"):' in src_single)
check("wire: launch-roblox does not patch (no status change)",
      src_single.count("patch_remote_status") == 2)

src_create = inspect.getsource(app.create_remote_instance)
check("wire: create_remote_instance patches on 200",
      "patch_remote_status(provider_id, name, True)" in src_create
      and src_create.index("patch_remote_status(provider_id, name, True)")
      < src_create.index('return 200, {"ok": True'))

src_handler = inspect.getsource(app.Handler)
check("wire: remote delete prunes the cache entry on 200",
      "drop_remote_status(prov, name)" in src_handler)

# ---------------- functional: the real fallback path ----------------
orig_wing = app.wing_request
orig_providers = app.load_providers
try:
    def fake_providers():
        return {"p1": {"last_seen": time.time(), "name": "wing-1"}}
    app.load_providers = fake_providers

    # baseline: failing fetch (502, the real wing_request contract — it never
    # raises) -> last-known-good (running=True) is served
    reset_cache()
    app._remote_status_cache["p1"] = (time.monotonic(), {
        ("p1", "n1"): {"name": "n1", "running": True, "setup": "ready"}})
    def fail_wing(*a, **k):
        return (502, {"error": "node wedged"})
    app.wing_request = fail_wing
    got = app.fetch_remote_statuses(app.load_providers())
    check("func: failing fetch serves last-known-good",
          got.get(("p1", "n1"), {}).get("running") is True)

    # after a Stop, the patched snapshot serves running=False instead
    app.patch_remote_status("p1", "n1", False)
    got = app.fetch_remote_statuses(app.load_providers())
    check("func: post-Stop patch flows through the fallback path",
          got.get(("p1", "n1"), {}).get("running") is False)

    # a healthy fetch replaces the optimistic patch wholesale
    def ok_wing(*a, **k):
        return (200, {"instances": [{"name": "n1", "running": True,
                                     "setup": "ready"}]})
    app.wing_request = ok_wing
    got = app.fetch_remote_statuses(app.load_providers())
    check("func: successful fetch overwrites the patch",
          got.get(("p1", "n1")) == {"name": "n1", "running": True,
                                    "setup": "ready"})

    # drop: deleted instance contributes nothing even from cache
    reset_cache()
    app._remote_status_cache["p1"] = (time.monotonic(), {
        ("p1", "n1"): {"name": "n1", "running": True}})
    app.drop_remote_status("p1", "n1")
    app.wing_request = fail_wing
    got = app.fetch_remote_statuses(app.load_providers())
    check("func: dropped instance absent from fallback",
          ("p1", "n1") not in got)
finally:
    app.wing_request = orig_wing
    app.load_providers = orig_providers
    reset_cache()

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
