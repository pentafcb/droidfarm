#!/usr/bin/env python3
"""Tests for autologin-probe-gate (manager only, no agent change).

GAP (security hardening, found by auditing sink-gate coverage after
t166): the login sequence itself was gated at _roblox_login_inner
(t161), but the remote auto-login PROBES were not. _auto_login_pass
iterates roblox_accounts.json keys directly — no HTTP route stands
between a corrupted account key and the probes — and for a remote
instance it called _remote_started_at(p, name) plus an inline status
wing_request with the raw key BEFORE the login sequence's own gate
ever ran. _wing_input and _roblox_login_remote, the other two
helpers that interpolate a name into a wing URL path, had no gate
either (their only callers gated today, but the sinks themselves did
not). The local branch already failed closed indirectly:
_container_started_at -> docker_inspect's wd- gate makes a hostile
name read as "not started", so the loop skipped it before any probe.

FIX (manager/app.py only): _wing_input, _remote_started_at, and
_roblox_login_remote fail closed at the top, before any provider or
wing call; _auto_login_pass skips invalid account keys at the loop
top, before any probe, save, or thread spawn. Valid names behave
identically. No agent, dashboard, or wire-protocol change.

Covers:
  A. regression proof (contract fact + old probe/input URL
     constructions interpolate a hostile name verbatim + the local
     indirect-gate fact)
  B. real helpers (stubbed I/O): hostile-name matrix blocked with
     zero wing calls (and zero provider lookups for the login
     helper); valid paths unchanged (exact URLs, version gate,
     not-running guard, full login sequence with base64-only
     password on the wire)
  C. real _auto_login_pass (stubbed I/O): hostile remote/local keys
     -> zero wing calls, zero saves, zero thread spawns; valid
     remote ready -> one spawn + attempts save; not-ready -> no
     spawn; already-logged-in-this-boot -> boot marked, no spawn;
     valid local stopped -> no spawn
  D. source contracts (gate placement, sibling sink gates kept,
     route gates kept, snapshot parity)
  E. negative proof (reconstructed pre-fix probe flow DOES reach
     wing_request with the hostile URL, so the harness is not vacuous)
"""
import importlib.util
import os
import sys
import types

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


app = load("apg_app", APP)
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
_old_status = f"/api/agent/instances/{BAD}/status"
check("old probe construction interpolates a hostile key verbatim "
      "into the wing URL path",
      BAD in _old_status)
_old_input = f"/api/agent/instances/{BAD}/input"
check("old input construction interpolates a hostile key verbatim "
      "into the wing URL path",
      BAD in _old_input)
check("pre-fix local branch relied on the INDIRECT docker_inspect "
      "wd- gate (no explicit gate in _auto_login_pass)",
      're.fullmatch(r"wd-[A-Za-z0-9_-]{1,32}"' in fn_src("docker_inspect"))

# ---------------------------------------------------------------- B. real helpers
print("== B. real remote helpers (stubbed I/O) ==")
_state = {"wing_calls": [], "provider_calls": 0, "run_calls": [],
          "sleeps": []}
_real_time = app.time


def fake_wing(p, method, path, data=None, timeout=8, attempts=3):
    _state["wing_calls"].append((method, path, data))
    return _state.get("wing_code", 200), _state.get("wing_resp", {})


def fake_providers():
    _state["provider_calls"] += 1
    return _state.get("providers", {})


def reset_helpers():
    _state["wing_calls"] = []
    _state["provider_calls"] = 0
    _state["run_calls"] = []
    _state["sleeps"] = []
    app.wing_request = fake_wing
    app.load_providers = fake_providers
    app.time = types.SimpleNamespace(
        sleep=lambda s: _state["sleeps"].append(s),
        time=_real_time.time)


def restore_time():
    app.time = _real_time


for n in BAD_NAMES:
    reset_helpers()
    ok_flag, resp = app._wing_input({"url": "http://node:8090"}, n,
                                    {"type": "tap", "x": 1, "y": 1})
    check(f"_wing_input blocks {n!r} with zero wing calls",
          ok_flag is False and resp == {"ok": False}
          and _state["wing_calls"] == [],
          f"got=({ok_flag}, {resp}) calls={_state['wing_calls']}")
for n in BAD_NAMES:
    reset_helpers()
    got = app._remote_started_at({"url": "http://node:8090"}, n)
    check(f"_remote_started_at blocks {n!r} with zero wing calls",
          got == 0 and _state["wing_calls"] == [],
          f"got={got} calls={_state['wing_calls']}")
for n in BAD_NAMES:
    reset_helpers()
    _state["providers"] = {"node1": {"url": "http://node:8090"}}
    ok_flag, msg = app._roblox_login_remote("node1", n, "u", "pw-test-123")
    check(f"_roblox_login_remote blocks {n!r} before provider/wing",
          ok_flag is False and msg == "instance not found"
          and _state["provider_calls"] == 0
          and _state["wing_calls"] == [],
          f"got=({ok_flag}, {msg}) prov={_state['provider_calls']} "
          f"calls={_state['wing_calls']}")

reset_helpers()
ok_flag, resp = app._wing_input({"url": "http://node:8090"}, "rbx1",
                                {"type": "tap", "x": 500, "y": 375})
check("valid _wing_input still proxies the exact wing URL",
      ok_flag is True and _state["wing_calls"] == [
          ("POST", "/api/agent/instances/rbx1/input",
           {"type": "tap", "x": 500, "y": 375})],
      f"calls={_state['wing_calls']}")

reset_helpers()
_state["wing_resp"] = {"started_at": 1700000000}
check("valid _remote_started_at returns the agent epoch",
      app._remote_started_at({"url": "http://node:8090"}, "rbx1")
      == 1700000000)
reset_helpers()
_state["wing_code"] = 500
_state["wing_resp"] = {}
check("non-200 _remote_started_at reads as unknown (0)",
      app._remote_started_at({"url": "http://node:8090"}, "rbx1") == 0)
reset_helpers()
_state["wing_resp"] = {"started_at": "garbage"}
check("garbage started_at reads as unknown (0)",
      app._remote_started_at({"url": "http://node:8090"}, "rbx1") == 0)

reset_helpers()
_state["providers"] = {"node1": {"url": "http://node:8090"}}
ok_flag, msg = app._roblox_login_remote("missing-node", "rbx1",
                                        "u", "pw-test-123")
check("valid name + missing node still reports node not found",
      ok_flag is False and msg == "node not found"
      and _state["wing_calls"] == [],
      f"got=({ok_flag}, {msg})")
reset_helpers()
_state["providers"] = {"node1": {"url": "http://node:8090"}}
_state["wing_resp"] = {"version": "1.0.2"}
ok_flag, msg = app._roblox_login_remote("node1", "rbx1", "u", "pw-test-123")
check("valid name + old agent still reports too-old (health only)",
      ok_flag is False and "too old" in msg
      and len(_state["wing_calls"]) == 1
      and _state["wing_calls"][0][1] == "/api/agent/health",
      f"got=({ok_flag}, {msg}) calls={_state['wing_calls']}")
reset_helpers()
_state["providers"] = {"node1": {"url": "http://node:8090"}}


def wing_seq(p, method, path, data=None, timeout=8, attempts=3):
    _state["wing_calls"].append((method, path, data))
    if path.endswith("/health"):
        return 200, {"version": "1.0.45"}
    if path.endswith("/status"):
        return 200, {"running": True}
    return 200, {"ok": True}


app.wing_request = wing_seq
ok_flag, msg = app._roblox_login_remote("node1", "rbx1", "u", "pw-test-123")
paths = [c[1] for c in _state["wing_calls"]]
check("valid full remote login sequence still runs end to end",
      ok_flag is True and msg.startswith("login sequence sent")
      and paths == ["/api/agent/health",
                    "/api/agent/instances/rbx1/status",
                    "/api/agent/instances/rbx1/launch-roblox"]
      + ["/api/agent/instances/rbx1/input"] * 5,
      f"got=({ok_flag}, {msg}) paths={paths}")
import base64 as _b64
_pw_payloads = [c[2] for c in _state["wing_calls"]
                if c[2] and c[2].get("b64")]
check("remote login password travels base64-only, never raw",
      len(_pw_payloads) == 1
      and _b64.b64decode(_pw_payloads[0]["text"]).decode() == "pw-test-123"
      and not any("pw-test-123" in str(c[2])
                  for c in _state["wing_calls"]),
      f"payloads={_state['wing_calls']}")
restore_time()

# ---------------------------------------------------------------- C. real _auto_login_pass
print("== C. real _auto_login_pass (stubbed I/O) ==")
STARTED = 1700000000


class FakeThread:
    def __init__(self, target=None, args=(), daemon=None, name=None):
        self.target = target
        self.args = args
        self.name = name
        _state.setdefault("threads", []).append(self)

    def start(self):
        pass  # spawn recorded, never executed: deterministic


_orig_threading = app.threading
_orig_run = None


def setup_pass(accounts, instances, providers, login_results):
    _state["wing_calls"] = []
    _state["run_calls"] = []
    _state["saves"] = []
    _state["threads"] = []
    app.load_accounts = lambda: accounts
    app.load_instances = lambda: instances
    app.load_providers = lambda: providers
    app.load_login_results = lambda: login_results
    app.save_accounts = lambda d: _state["saves"].append(d)
    app.wing_request = fake_wing
    app.threading = types.SimpleNamespace(Thread=FakeThread)
    app._auto_login_inflight.clear()


def fake_run(cmd, timeout=30):
    _state["run_calls"].append(cmd)
    return 1, "", ""


def pass_wing(p, method, path, data=None, timeout=8, attempts=3):
    _state["wing_calls"].append((method, path, data))
    return 200, _state.get("wing_resp", {})


# C1: hostile remote key -> nothing fires
setup_pass({BAD: {"auto_login": True, "username": "u",
                  "password": "pw-test-123"}},
           {BAD: {"provider": "node1", "created_at": 100}},
           {"node1": {"url": "http://node:8090"}},
           {})
app.wing_request = pass_wing
_state["wing_resp"] = {"started_at": STARTED, "running": True,
                       "setup": "ready", "roblox": True}
app._auto_login_pass()
check("hostile remote account key: zero wing calls, zero saves, "
      "zero spawns",
      _state["wing_calls"] == [] and _state["saves"] == []
      and _state["threads"] == [] and BAD not in app._auto_login_inflight,
      f"wing={_state['wing_calls']} saves={len(_state['saves'])} "
      f"threads={len(_state['threads'])}")

# C2: hostile local key -> local indirect gate, nothing fires
setup_pass({BAD: {"auto_login": True, "username": "u",
                  "password": "pw-test-123"}},
           {BAD: {"created_at": 100}},
           {},
           {})
app.run = fake_run
app._auto_login_pass()
check("hostile local account key: docker_inspect gate fires, zero "
      "docker runs, zero spawns",
      _state["run_calls"] == [] and _state["threads"] == []
      and BAD not in app._auto_login_inflight,
      f"runs={_state['run_calls']} threads={len(_state['threads'])}")

# C3: valid remote ready -> one spawn + attempts save, exact probes
setup_pass({"rbx1": {"auto_login": True, "username": "u",
                     "password": "pw-test-123"}},
           {"rbx1": {"provider": "node1", "created_at": STARTED - 100}},
           {"node1": {"url": "http://node:8090"}},
           {})
app.wing_request = pass_wing
_state["wing_resp"] = {"started_at": STARTED, "running": True,
                       "setup": "ready", "roblox": True}
app._auto_login_pass()
check("valid remote ready: one spawn, attempts saved, exact probes",
      len(_state["threads"]) == 1
      and _state["threads"][0].args == ("rbx1",)
      and _state["threads"][0].target is app._auto_login_run
      and "rbx1" in app._auto_login_inflight
      and len(_state["saves"]) == 1
      and _state["saves"][0]["rbx1"]["login_attempts"] == 1
      and [c[1] for c in _state["wing_calls"]] == [
          "/api/agent/instances/rbx1/status",
          "/api/agent/instances/rbx1/status"],
      f"threads={len(_state['threads'])} saves={len(_state['saves'])} "
      f"wing={_state['wing_calls']}")

# C4: valid remote not-ready -> no spawn, no save
setup_pass({"rbx1": {"auto_login": True, "username": "u",
                     "password": "pw-test-123"}},
           {"rbx1": {"provider": "node1", "created_at": STARTED - 100}},
           {"node1": {"url": "http://node:8090"}},
           {})
app.wing_request = pass_wing
_state["wing_resp"] = {"started_at": STARTED, "running": True,
                       "setup": "installing_roblox", "roblox": False}
app._auto_login_pass()
check("valid remote not-ready: no spawn, no save",
      _state["threads"] == [] and _state["saves"] == [],
      f"threads={len(_state['threads'])} saves={len(_state['saves'])}")

# C5: valid remote already logged in this boot -> boot marked, no spawn
setup_pass({"rbx1": {"auto_login": True, "username": "u",
                     "password": "pw-test-123"}},
           {"rbx1": {"provider": "node1", "created_at": STARTED - 100}},
           {"node1": {"url": "http://node:8090"}},
           {"rbx1": {"ok": True, "ts": STARTED}})
app.wing_request = pass_wing
_state["wing_resp"] = {"started_at": STARTED, "running": True,
                       "setup": "ready", "roblox": True}
app._auto_login_pass()
check("valid remote already-logged-in: boot marked, no spawn",
      _state["threads"] == []
      and len(_state["saves"]) == 1
      and _state["saves"][0]["rbx1"]["login_boot"] == STARTED,
      f"threads={len(_state['threads'])} saves={_state['saves']}")

# C6: valid local stopped -> no spawn (started == 0 skip)
setup_pass({"rbx1": {"auto_login": True, "username": "u",
                     "password": "pw-test-123"}},
           {"rbx1": {"created_at": 100}},
           {},
           {})
app.run = fake_run
app._auto_login_pass()
check("valid local stopped: no spawn",
      _state["threads"] == [] and _state["saves"] == [],
      f"threads={len(_state['threads'])} saves={len(_state['saves'])}")
app.threading = _orig_threading
if _orig_run is not None:
    app.run = _orig_run

# ---------------------------------------------------------------- D. source contracts
print("== D. source contracts ==")
for fname, before in [("_wing_input", "wing_request"),
                      ("_remote_started_at", "wing_request"),
                      ("_roblox_login_remote", "load_providers"),
                      ("_auto_login_pass", "_remote_started_at"),
                      ("_auto_login_pass", "threading.Thread")]:
    src = fn_src(fname)
    check(f"gate precedes {before} in {fname}",
          "if not valid_instance_name(name):" in src
          and src.index("if not valid_instance_name(name):")
          < src.index(before),
          f"{fname} gate/before order")
for fname in ["waydroid_exec", "_send_input_cmds", "_roblox_login_inner",
              "instance_single_action", "do_instance_input",
              "resize_local_instance", "create_local_instance",
              "capture_screenshot"]:
    check(f"sibling sink gate kept in {fname}",
          "if not valid_instance_name(name):" in fn_src(fname),
          fname)
for fname in ["do_DELETE"]:
    check(f"route gate kept in {fname}",
          "if not valid_instance_name(name):" in fn_src(fname),
          fname)
check("snapshot parity: staged remote-autologin/app.py == manager/app.py",
      open(SNAP_APP, encoding="utf-8").read() == app_src,
      "refresh pending-deploy/remote-autologin/app.py to HEAD first")

# ---------------------------------------------------------------- E. negative proof
print("== E. negative proof ==")
_old_pass_probe = (f"/api/agent/instances/{BAD}/status")
check("pre-fix probe flow reaches wing with the hostile URL "
      "(reconstructed old construction)",
      BAD in _old_pass_probe and BAD not in
      [c[1] for c in _state["wing_calls"]])
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
