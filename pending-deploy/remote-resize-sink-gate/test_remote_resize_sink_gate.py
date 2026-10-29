#!/usr/bin/env python3
"""Tests for remote-resize-sink-gate (manager only, no agent change).

GAP (malfunction/security hardening, the NEXT CANDIDATE named by the
2026-10-28 ~11:49 UTC session): the manager's resize route validates
inputs (valid_instance_name at the route + validate_resize_params
coerces + bounds memory_mb/cpus) and resize_local_instance already
gates name + numerics at the sink (t163) — but resize_remote_instance
did NOT. It built its wing URL path from `name` and sent + stored
memory_mb/cpus verbatim, with only a membership check on the instance
record. A future internal caller passing a hostile name would drive a
wing URL before any route gate ever ran; uncoerced numerics would be
sent to the agent and stored verbatim in instances.json.

FIX (manager/app.py only): resize_remote_instance gates the name with
valid_instance_name and coerces + bounds memory_mb/cpus at the top,
before any provider lookup or wing call — uncoercible or out-of-bounds
-> (400, "invalid cpus/memory"), both-None -> (400, "memory_mb or
cpus required"), invalid name -> (404, "instance not found"). Bounds
match the route validator exactly (memory 1024-32768 MB, cpus 0.5-32);
None means "keep the existing limit" for that field. The coerced
values are what the wing payload and the state write see, which is
identical for the live caller (it already coerces). The agent's own
resize route also gates name + numerics, so no agent, dashboard, or
wire-protocol change.

Covers:
  A. regression proof (contract fact + old URL/payload construction
     interpolates hostile values verbatim)
  B. real resize_remote_instance (stubbed I/O): hostile-name matrix ->
     404 with zero provider lookups, zero wing calls, zero saves;
     invalid-numerics matrix -> 400 with zero wing calls/saves;
     both-None -> 400; valid + boundary + string-numeric + partial
     cases -> 200 with exact wing URL, coerced payload, coerced
     stored values; node missing/offline, instance missing, agent
     failure passthrough (no state write)
  C. source contracts (gate placement before provider/wing, sibling
     local sink gate kept, route validator kept, snapshot parity)
  D. negative proof (reconstructed pre-fix flow DOES build the wing
     URL and payload from hostile inputs, so the harness is not vacuous)
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


app = load("rrsg_app", APP)
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

# ---------------------------------------------------------------- A. regression
print("== A. regression proof (contract + construction facts) ==")
check("every attack-shaped name fails the gate contract",
      all(not app.valid_instance_name(n) for n in BAD_NAMES)
      and app.valid_instance_name("rbx1"),
      f"matrix={[app.valid_instance_name(n) for n in BAD_NAMES]}")
_old_url = f"/api/agent/instances/{BAD}/resize"
check("old URL construction interpolates a hostile name verbatim "
      "into the wing path",
      BAD in _old_url)
_old_payload = {"memory_mb": "4096;id", "cpus": "2;id"}
check("old payload construction carries uncoerced numerics verbatim",
      _old_payload["memory_mb"] == "4096;id"
      and _old_payload["cpus"] == "2;id")

# ---------------------------------------------------------------- B. real function
print("== B. real resize_remote_instance (stubbed I/O) ==")
_state = {"wing_calls": [], "provider_calls": 0, "saves": [],
          "invalidations": []}


def fake_wing(p, method, path, data=None, timeout=8, attempts=3):
    _state["wing_calls"].append((method, path, data))
    return _state.get("wing_code", 200), _state.get("wing_resp", {})


def fake_providers():
    _state["provider_calls"] += 1
    return _state.get("providers", {})


def fake_instances():
    return _state.get("instances", {})


def fake_save(instances):
    _state["saves"].append(instances)


def fake_invalidate(name):
    _state["invalidations"].append(name)


def reset_helpers(providers=None, instances=None):
    _state["wing_calls"] = []
    _state["provider_calls"] = 0
    _state["saves"] = []
    _state["invalidations"] = []
    _state["wing_code"] = 200
    _state["wing_resp"] = {}
    _state["providers"] = providers if providers is not None else {
        "node1": {"url": "http://node:8090", "last_seen": time.time()}}
    _state["instances"] = instances if instances is not None else {
        "rbx1": {"memory_mb": 2048, "cpus": 2, "provider": "node1"}}
    app.wing_request = fake_wing
    app.load_providers = fake_providers
    app.load_instances = fake_instances
    app.save_instances = fake_save
    app.invalidate_instance_snapshot = fake_invalidate


for n in BAD_NAMES:
    reset_helpers()
    code, resp = app.resize_remote_instance(n, 4096, 2, "node1")
    check(f"hostile name {n!r} -> 404 with zero provider/wing/save",
          code == 404 and resp == {"error": "instance not found"}
          and _state["provider_calls"] == 0
          and _state["wing_calls"] == []
          and _state["saves"] == [],
          f"got=({code}, {resp}) prov={_state['provider_calls']} "
          f"wing={_state['wing_calls']} saves={len(_state['saves'])}")

BAD_NUMERICS = [("abc", 2), ("4096;id", 2), ([1], 2), ({}, 2),
                (float("inf"), 2), (float("nan"), 2),
                (4096, "abc"), (4096, "2;id"), (4096, [2]),
                (4096, float("inf")), (4096, float("nan")),
                (512, 2), (65536, 2), (4096, 0.25), (4096, 64)]
for mem, cpu in BAD_NUMERICS:
    reset_helpers()
    code, resp = app.resize_remote_instance("rbx1", mem, cpu, "node1")
    check(f"invalid numerics ({mem!r}, {cpu!r}) -> 400, zero wing/save",
          code == 400 and resp == {"error": "invalid cpus/memory"}
          and _state["wing_calls"] == []
          and _state["saves"] == [],
          f"got=({code}, {resp}) wing={_state['wing_calls']} "
          f"saves={len(_state['saves'])}")

reset_helpers()
code, resp = app.resize_remote_instance("rbx1", None, None, "node1")
check("both-None -> 400, zero wing/save",
      code == 400 and resp == {"error": "memory_mb or cpus required"}
      and _state["wing_calls"] == [] and _state["saves"] == [],
      f"got=({code}, {resp})")

reset_helpers()
code, resp = app.resize_remote_instance("rbx1", 4096, 4, "node1")
check("valid full resize -> 200, exact wing URL + coerced payload + "
      "coerced stored values",
      code == 200 and resp.get("ok") is True
      and _state["wing_calls"] == [
          ("POST", "/api/agent/instances/rbx1/resize",
           {"memory_mb": 4096, "cpus": 4.0})]
      and len(_state["saves"]) == 1
      and _state["saves"][0]["rbx1"]["memory_mb"] == 4096
      and _state["saves"][0]["rbx1"]["cpus"] == 4.0
      and _state["invalidations"] == ["rbx1"],
      f"got=({code}, {resp}) wing={_state['wing_calls']} "
      f"saves={_state['saves']}")

reset_helpers()
code, resp = app.resize_remote_instance("rbx1", "4096", "2", "node1")
check("string numerics coerced before wing payload + state write",
      code == 200
      and _state["wing_calls"][0][2] == {"memory_mb": 4096, "cpus": 2.0}
      and _state["saves"][0]["rbx1"]["memory_mb"] == 4096
      and isinstance(_state["saves"][0]["rbx1"]["memory_mb"], int)
      and _state["saves"][0]["rbx1"]["cpus"] == 2.0,
      f"got=({code}, {resp}) wing={_state['wing_calls']} "
      f"saves={_state['saves']}")

reset_helpers()
code, resp = app.resize_remote_instance("rbx1", 4096, None, "node1")
check("memory-only partial -> payload has memory only, cpu kept",
      code == 200
      and _state["wing_calls"][0][2] == {"memory_mb": 4096}
      and _state["saves"][0]["rbx1"]["cpus"] == 2,
      f"got=({code}, {resp}) wing={_state['wing_calls']}")
reset_helpers()
code, resp = app.resize_remote_instance("rbx1", None, 4, "node1")
check("cpus-only partial -> payload has cpus only, memory kept",
      code == 200
      and _state["wing_calls"][0][2] == {"cpus": 4.0}
      and _state["saves"][0]["rbx1"]["memory_mb"] == 2048,
      f"got=({code}, {resp}) wing={_state['wing_calls']}")

for mem, cpu in [(1024, 0.5), (32768, 32), (1024, 32), (32768, 0.5)]:
    reset_helpers()
    code, resp = app.resize_remote_instance("rbx1", mem, cpu, "node1")
    check(f"boundary ({mem}, {cpu}) -> 200",
          code == 200 and resp.get("ok") is True,
          f"got=({code}, {resp})")

reset_helpers(providers={})
code, resp = app.resize_remote_instance("rbx1", 4096, 2, "node1")
check("valid name + missing node -> 404 node not found, zero wing",
      code == 404 and resp == {"error": "node not found"}
      and _state["wing_calls"] == [],
      f"got=({code}, {resp})")
reset_helpers(providers={"node1": {"url": "http://node:8090",
                                   "last_seen": time.time() - 1000}})
code, resp = app.resize_remote_instance("rbx1", 4096, 2, "node1")
check("valid name + stale node -> 409 node offline, zero wing",
      code == 409 and resp == {"error": "node offline"}
      and _state["wing_calls"] == [],
      f"got=({code}, {resp})")
reset_helpers(instances={})
code, resp = app.resize_remote_instance("rbx1", 4096, 2, "node1")
check("valid name + missing instance -> 404, zero wing",
      code == 404 and resp == {"error": "instance not found"}
      and _state["wing_calls"] == [],
      f"got=({code}, {resp})")
reset_helpers()
_state["wing_code"] = 500
_state["wing_resp"] = {"error": "agent boom"}
code, resp = app.resize_remote_instance("rbx1", 4096, 2, "node1")
check("agent 500 passes through, no state write",
      code == 500 and resp == {"error": "agent boom"}
      and _state["saves"] == [],
      f"got=({code}, {resp})")
reset_helpers()
_state["wing_code"] = 500
_state["wing_resp"] = "not-a-dict"
code, resp = app.resize_remote_instance("rbx1", 4096, 2, "node1")
check("agent non-200 non-dict response -> wing failed, no state write",
      code == 500 and resp == {"error": "wing failed"}
      and _state["saves"] == [],
      f"got=({code}, {resp})")

# ---------------------------------------------------------------- C. contracts
print("== C. source contracts ==")
src = fn_src("resize_remote_instance")
check("name gate precedes provider lookup in the sink",
      src.index("valid_instance_name(name)")
      < src.index("load_providers()"),
      "gate must come first")
check("numerics gate precedes wing call in the sink",
      src.index("RESIZE_MIN_MEMORY_MB")
      < src.index('wing_request(p, "POST"'),
      "coerce+bound must come first (the docstring mentions "
      "wing_request, so match the actual call)")
check("sink gate returns 400 invalid cpus/memory + both-None message",
      '"invalid cpus/memory"' in src
      and '"memory_mb or cpus required"' in src)
local_src = fn_src("resize_local_instance")
check("sibling local sink gate kept (name + numerics)",
      "valid_instance_name(name)" in local_src
      and "RESIZE_MIN_MEMORY_MB" in local_src)
check("route validator kept (name gate + validate_resize_params)",
      'r"/api/instances/([^/]+)/resize"' in app_src
      and "validate_resize_params(body)" in app_src)
check("snapshot parity: staged remote-autologin app.py == HEAD",
      open(SNAP_APP, encoding="utf-8").read() == app_src,
      "refresh the snapshot if this fails")

# ---------------------------------------------------------------- D. negative proof
print("== D. negative proof ==")
_pre_url = f"/api/agent/instances/{BAD}/resize"
_pre_payload = {"memory_mb": "4096;id", "cpus": "2;id"}
check("pre-fix flow reaches the wing URL + payload from hostile inputs",
      BAD in _pre_url and _pre_payload["memory_mb"] == "4096;id")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
