#!/usr/bin/env python3
"""Tests for instance-record-guard (manager only, no agent change).

GAP (the instance-side twin of t154, flagged as the next audit
candidate in the t154 entry): t152/t153 fixed the instances FILE shape
and unreadability and t154 fixed PROVIDER records, but an INSTANCE
record inside a valid dict file can still be the wrong shape — a hand
edit or partial write can leave a string record, a string provider, or
an empty-string provider. Every caller that trusted the record shape
then crashed the handler thread with no response:

  1. instance_single_action: `instances[name].get("provider")` raised
     AttributeError on a string record (start/stop/restart/launch/
     login-all all dropped for that instance).
  2. GET /api/instances loop: one string record raised AttributeError
     on `inst.get("provider")` and killed the WHOLE fleet poll; a
     string provider record crashed the pname lookup the same way.
  3. _handle_input / _handle_webrtc_offer / _handle_ws / _handle_ws_h264
     / resize route / roblox-login route / DELETE instance: the
     `load_instances().get(name, {}).get("provider")` idiom crashed on
     a string record (the {} default only covers a MISSING record).
  4. resize_local / resize_remote write-backs: `inst["memory_mb"] = ...`
     raised TypeError on a string record after docker/agent accepted.
  5. create_remote_instance IP pick: an agent returning a dict (not a
     list) for "instances" iterated string keys, and a string item
     crashed on `.get("ip")` — the create request dropped.
  6. Create-path helpers next_instance_id / used_binders / next_free_ip
     and pick_provider / fleet_headroom sums: all called `.get` on
     every instance record, so one bad record dropped a create, an
     auto-pick, or a fleet plan.

FIX (manager/app.py only): instance_provider_of(record) — missing
record or missing provider field means "local" (historic default);
a non-dict record, or a present but non-string/empty provider, is
invalid -> None, and every caller treats None as "instance not found"
(never as local, never as a crash). The /api/instances loop skips bad
records (like the t154 providers list). Valid records behave
identically; no dashboard, agent, or wire-protocol change.

Covers:
  A. regression proof (old idiom necessarily crashed on these shapes)
  B. helper instance_provider_of
  C. real instance_single_action (local + remote, stubbed docker/wing)
  D. real resize_local / resize_remote (stubbed docker/wing)
  E. real create_remote_instance IP pick over agent-shape variants
  F. real create-path helpers + pick_provider + fleet_headroom
  G. real roblox_login provider branch + do_instance_input node guard
  H. source contracts (handlers + list loop) + snapshot parity
"""
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
AGENT = os.path.join(REPO, "wing", "agent.py")
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


app = load("irg_app", APP)
agent = load("irg_agent", AGENT)
app_src = open(APP, encoding="utf-8").read()


def fn_src(name):
    start = app_src.index(f"def {name}(")
    try:
        end = app_src.index("\ndef ", start + 1)
    except ValueError:
        end = len(app_src)
    return app_src[start:end]


NOW = time.time()
RECENT = {"url": "http://10.0.0.9:8090", "agent_key": "k",
          "last_seen": NOW, "stats": {"ram_total_mb": 32000,
                                       "ram_percent": 50}}

print("== A. regression proof ==")
try:
    "oops".get("provider")
    bad("str record .get raises AttributeError", "no exception")
except AttributeError:
    ok("str record .get raises AttributeError")
try:
    {"instances": {"a": 1}}.get("instances")[0]
    bad("dict instances iterates keys, not dicts", "no exception")
except (KeyError, TypeError):
    ok("dict instances iterates keys, not dicts")
try:
    s = "oops"
    s["memory_mb"] = 2048
    bad("str record item assignment raises TypeError", "no exception")
except TypeError:
    ok("str record item assignment raises TypeError")

print("== B. helper ==")
check("dict local passes through",
      app.instance_provider_of({"provider": "local"}) == "local")
check("dict remote passes through",
      app.instance_provider_of({"provider": "srt1"}) == "srt1")
check("missing provider field -> local",
      app.instance_provider_of({}) == "local")
check("missing record -> local",
      app.instance_provider_of(None) == "local")
check("str record -> None",
      app.instance_provider_of("oops") is None)
check("list record -> None",
      app.instance_provider_of(["x"]) is None)
check("int provider -> None",
      app.instance_provider_of({"provider": 7}) is None)
check("empty provider -> None",
      app.instance_provider_of({"provider": ""}) is None)
check("None provider -> None",
      app.instance_provider_of({"provider": None}) is None)


def stub_env(providers=None, instances=None, wing=None):
    saved = {}
    for attr in ("load_providers", "load_instances", "save_instances",
                 "wing_request", "get_host_stats", "container_status",
                 "roblox_login", "check_roblox", "launch_roblox",
                 "invalidate_instance_snapshot", "patch_remote_status",
                 "instance_resources", "run", "load_accounts",
                 "count_loop_devices", "waydroid_image_present"):
        saved[attr] = getattr(app, attr, None)
    app.load_providers = lambda: providers or {}
    held = {"instances": dict(instances or {})}

    def _load_instances():
        return held["instances"]

    def _save_instances(data):
        held["instances"] = dict(data)

    app.load_instances = _load_instances
    app.save_instances = _save_instances
    calls = []
    if wing is not None:
        def _wing(p, method, path, data=None, timeout=8, attempts=3):
            calls.append((method, path))
            return wing(p, method, path, data)
    else:
        def _wing(p, method, path, data=None, timeout=8, attempts=3):
            calls.append((method, path))
            return 502, {"error": "should not be called"}
    app.wing_request = _wing
    app.get_host_stats = lambda: {"ram_total_mb": 32000, "ram_percent": 10}
    app.container_status = lambda cname: "running"
    app.roblox_login = lambda name, by="manual": (True, "ok")
    app.check_roblox = lambda name: True
    app.launch_roblox = lambda name: True
    app.invalidate_instance_snapshot = lambda name: None
    app.patch_remote_status = lambda *a: None
    app.instance_resources = lambda name: None
    app.run = lambda cmd, timeout=60: (0, "", "")
    app.load_accounts = lambda: {}
    app.count_loop_devices = lambda: 8
    app.waydroid_image_present = lambda: True
    return saved, held, calls


def restore(saved):
    for attr, val in saved.items():
        if val is None:
            try:
                delattr(app, attr)
            except AttributeError:
                pass
        else:
            setattr(app, attr, val)


print("== C. instance_single_action ==")
saved, held, calls = stub_env(instances={"bad": "oops"})
try:
    code, resp = app.instance_single_action("bad", "start")
    check("str record -> 404 instance not found",
          (code, resp.get("error")) == (404, "instance not found"),
          repr((code, resp)))
finally:
    restore(saved)
saved, held, calls = stub_env(instances={"bad": {"provider": 7}})
try:
    code, resp = app.instance_single_action("bad", "stop")
    check("int provider -> 404 instance not found", code == 404,
          repr((code, resp)))
finally:
    restore(saved)
saved, held, calls = stub_env(instances={"rbx1": {"provider": "local"}})
try:
    code, resp = app.instance_single_action("rbx1", "launch-roblox")
    check("valid local record -> 200", code == 200, repr((code, resp)))
finally:
    restore(saved)
saved, held, calls = stub_env(
    providers={"srt1": "oops"}, instances={"srt1x": {"provider": "srt1"}})
try:
    code, resp = app.instance_single_action("srt1x", "start")
    check("str provider record -> 404 node not found",
          (code, resp.get("error")) == (404, "node not found"),
          repr((code, resp)))
    check("str provider record -> wing never called", calls == [],
          repr(calls))
finally:
    restore(saved)

def wing_start(p, method, path, data):
    return 200, {"ok": True}

saved, held, calls = stub_env(
    providers={"srt1": dict(RECENT)},
    instances={"srt1x": {"provider": "srt1"}}, wing=wing_start)
try:
    code, resp = app.instance_single_action("srt1x", "start")
    check("valid remote record -> 200 via wing", code == 200,
          repr((code, resp)))
finally:
    restore(saved)

print("== D. resize ==")
saved, held, calls = stub_env(instances={"bad": "oops"})
try:
    code, resp = app.resize_local_instance("bad", 2048, 2)
    check("resize_local str record -> 404", code == 404, repr((code, resp)))
finally:
    restore(saved)
saved, held, calls = stub_env(instances={"rbx1": {"provider": "local",
                                                  "memory_mb": 2048,
                                                  "cpus": 2}})
try:
    code, resp = app.resize_local_instance("rbx1", 4096, 4)
    check("resize_local valid -> 200", code == 200, repr((code, resp)))
    check("resize_local write-back applied",
          held["instances"]["rbx1"]["memory_mb"] == 4096, repr(held))
finally:
    restore(saved)
saved, held, calls = stub_env(
    providers={"srt1": dict(RECENT)}, instances={"srt1x": "oops"})
try:
    code, resp = app.resize_remote_instance("srt1x", 2048, 2, "srt1")
    check("resize_remote str instance -> 404", code == 404,
          repr((code, resp)))
finally:
    restore(saved)
saved, held, calls = stub_env(
    providers={"srt1": dict(RECENT)},
    instances={"srt1x": {"provider": "srt1", "memory_mb": 2048, "cpus": 2}},
    wing=wing_start)
try:
    code, resp = app.resize_remote_instance("srt1x", 4096, 4, "srt1")
    check("resize_remote valid -> 200", code == 200, repr((code, resp)))
    check("resize_remote write-back applied",
          held["instances"]["srt1x"]["memory_mb"] == 4096, repr(held))
finally:
    restore(saved)

print("== E. create_remote IP pick ==")

def wing_shape(shape):
    def _wing(p, method, path, data):
        if method == "GET":
            return 200, {"instances": shape}
        return 200, {}
    return _wing

for label, shape, want in (
        ("dict instances -> keys ignored, first IP free",
         {"10.20.0.11": {}}, "10.20.0.11"),
        ("string item ignored", ["oops"], "10.20.0.11"),
        ("dict item IP honoured", [{"ip": "10.20.0.11"}], "10.20.0.12"),
        ("string instances ignored", "10.20.0.11", "10.20.0.11"),
        ("None instances ignored", None, "10.20.0.11")):
    saved, held, calls = stub_env(providers={"srt1": dict(RECENT)},
                                  wing=wing_shape(shape))
    try:
        code, resp = app.create_remote_instance("n1", 2, 2048, "srt1")
        check(label, code == 200 and resp.get("ip") == want,
              repr((code, resp)))
    finally:
        restore(saved)

print("== F. create-path helpers + pick + headroom ==")
mixed = {"a": {"instance_id": 0, "ip": "10.20.0.11",
               "binders": ["binder"], "memory_mb": 2048,
               "provider": "local"},
         "bad": "oops",
         "c": {"instance_id": 2, "ip": "10.20.0.12",
               "binders": ["binder1"], "memory_mb": 1024,
               "provider": "local"}}
saved, held, calls = stub_env(instances=mixed)
try:
    check("next_instance_id skips str record (0,2 used -> 1)",
          app.next_instance_id(mixed) == 1)
    check("next_free_ip skips str record (range starts .10, first free)",
          app.next_free_ip(mixed) == "10.20.0.10")
    check("used_binders skips str record",
          app.used_binders(mixed) == {"binder", "binder1"})
    check("pick_provider survives str record, picks local",
          app.pick_provider({}, need_ram_mb=4096) == "local")
    hr = app.fleet_headroom(mixed, "local", 1, 2048, cpus=2)
    check("fleet_headroom survives str record",
          hr["current_limits_mb"] == 3072, repr(hr))
    check("fleet_headroom binder math skips str record",
          hr["binder_free_triplets"] is not None, repr(hr))
finally:
    restore(saved)

print("== G. roblox_login + do_instance_input ==")
saved, held, calls = stub_env(instances={"bad": "oops"})
try:
    app.roblox_login = saved["roblox_login"]  # test the REAL function
    app.load_accounts = lambda: {"bad": {"username": "u",
                                         "password": "p"}}
    okk, msg = app.roblox_login("bad")
    check("roblox_login str record -> (False, instance not found)",
          (okk, msg) == (False, "instance not found"), repr((okk, msg)))
finally:
    restore(saved)
saved, held, calls = stub_env(providers={"srt1": "oops"})
try:
    okk, resp = app.do_instance_input("n1", "srt1", {"type": "tap",
                                                     "x": 500, "y": 500})
    check("do_instance_input str provider -> node not found",
          (okk, resp.get("error")) == (False, "node not found"),
          repr((okk, resp)))
    check("do_instance_input str provider -> wing never called",
          calls == [], repr(calls))
finally:
    restore(saved)

print("== H. source contracts + snapshot parity ==")
for name in ("_handle_input", "_handle_webrtc_offer",
             "_handle_ws", "_handle_ws_h264"):
    body = fn_src(name)
    check(f"{name} uses instance_provider_of",
          "instance_provider_of(" in body, body[:200])
check("instance_single_action uses instance_provider_of",
      "instance_provider_of(" in fn_src("instance_single_action"))
check("resize_local uses instance_provider_of",
      "instance_provider_of(" in fn_src("resize_local_instance"))
check("create_remote IP loop guards list+dict",
      "isinstance(agent_instances, list)" in fn_src("create_remote_instance")
      and "isinstance(i, dict)" in fn_src("create_remote_instance"))
check("instances list loop skips non-dict records",
      "if not isinstance(inst, dict):\n                    continue" in app_src)
check("no remaining bare .get(name, {}).get(\"provider\") outside docstring",
      app_src.count('.get(name, {}).get("provider"') == 1)  # docstring only
check("agent untouched (still 1.0.45)",
      'VERSION = "1.0.45"' in open(AGENT, encoding="utf-8").read())
check("remote-autologin app.py snapshot == manager/app.py",
      open(SNAP_APP, encoding="utf-8").read() == app_src)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
