#!/usr/bin/env python3
"""Tests for action-sink-gate (manager only, no agent change).

GAP (malfunction/security hardening, found by auditing sink-gate
coverage after t163): the per-instance routes gate the instance name
(valid_instance_name) before calling instance_single_action, but the
shared core itself — used by the batch worker as well as the routes —
did NOT. Its only guard was membership in instances.json, and the
batch worker validates job names the same way (membership only). A
hand-edited or corrupted state key (e.g. "rbx1;id>/tmp/p") present in
instances.json would flow verbatim into the shell `docker start`,
`docker stop`, and `docker start` (restart) commands, and into the
wing_request URL path on the remote branch. The login (t161), create
(t162), and resize (t163) sinks are gated; the start/stop/restart
core was the remaining ungated shell sink in the shared path.

FIX (manager/app.py only): instance_single_action fails closed at
the top, before any docker call or state read — invalid name ->
(404, "instance not found"), indistinguishable from an unknown
instance, exactly like the routes. Valid names behave identically
(all real instance ids match the contract). No agent, dashboard, or
wire-protocol change.

Covers:
  A. regression proof (contract fact + old shell construction
     interpolates a hostile state key verbatim)
  B. real instance_single_action (stubbed I/O): hostile keys present
     in instances.json -> 404 with zero docker calls for
     start/stop/restart (local) and zero wing calls (remote);
     unknown valid name -> 404; valid start/stop/restart ->
     200 with exact docker commands in order; launch-roblox and
     roblox-login branches still behave; unknown action -> 400
  C. source contracts (gate placement, sibling sink gates kept,
     route gates kept, snapshot parity)
  D. negative proof (reconstructed pre-fix flow DOES reach docker
     with the hostile command, so the harness is not vacuous)
"""
import importlib.util
import os
import sys

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


app = load("asg_app", APP)
app_src = open(APP, encoding="utf-8").read()


def fn_src(fname):
    start = app_src.index(f"def {fname}(")
    try:
        end = app_src.index("\ndef ", start + 1)
    except ValueError:
        end = len(app_src)
    return app_src[start:end]


# ---------------------------------------------------------------- A. regression
print("== A. regression proof (contract + shell-construction facts) ==")
_old_cmd = "docker start wd-rbx1;id>/tmp/p"
check("old docker-start construction interpolates a hostile state key verbatim",
      "wd-rbx1;id>/tmp/p" in _old_cmd)
check("batch worker validates job names by instances.json membership only "
      "(no contract check) — the core was the last line of defence",
      "instance_single_action(name, action)" in app_src)
check("instance-name contract is ^[A-Za-z0-9_-]{1,32}$",
      '_INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")' in app_src)

# ---------------------------------------------------------------- B. real function
print("== B. real instance_single_action (stubbed I/O) ==")
_state = {"instances": {}, "run_calls": [], "wing_calls": [],
          "container": "running"}


def fake_load_instances():
    return dict(_state["instances"])


def fake_load_providers():
    return {"srt": {"url": "http://node:8090"}}


def reset():
    # rbx1 local, srt1 remote, plus hostile keys a corrupted instances.json
    # could actually contain — the gate must stop all of them.
    _state["instances"] = {
        "rbx1": {"provider": "local"},
        "srt1": {"provider": "srt"},
        "rbx1;id>/tmp/p": {"provider": "local"},
        "a b": {"provider": "local"},
        "../x": {"provider": "local"},
        "a" * 33: {"provider": "local"},
        "x;y": {"provider": "srt"},
        "": {"provider": "local"},
    }
    _state["run_calls"] = []
    _state["wing_calls"] = []
    _state["container"] = "running"
    app.load_instances = fake_load_instances
    app.load_providers = fake_load_providers
    app.instance_provider_of = lambda rec: (
        rec.get("provider") if isinstance(rec, dict) else None)
    app._prepare_pools_for_start = lambda: None
    app.invalidate_instance_snapshot = lambda name: None
    app.patch_remote_status = lambda *a: None
    app.container_status = lambda cname: _state["container"]
    app.check_roblox = lambda name: True
    app.launch_roblox = lambda name: True
    app.roblox_login = lambda name, by="manual": (True, "ok")

    def fake_run(cmd, timeout=None):
        _state["run_calls"].append(cmd)
        return (0, "", "")
    app.run = fake_run

    def fake_wing(p, method, path, timeout=None, **kw):
        _state["wing_calls"].append((method, path))
        return 200, {"ok": True}
    app.wing_request = fake_wing


def act(name, action):
    return app.instance_single_action(name, action)


HOSTILE = ["rbx1;id>/tmp/p", "a b", "../x", "a" * 33, "x;y", ""]
for name in HOSTILE:
    for action in ("start", "stop", "restart"):
        reset()
        code, resp = act(name, action)
        check(f"hostile key {name!r} + {action} -> 404, zero docker, "
              "zero wing",
              code == 404 and resp.get("error") == "instance not found"
              and _state["run_calls"] == [] and _state["wing_calls"] == [],
              f"code={code} resp={resp} runs={_state['run_calls']} "
              f"wing={_state['wing_calls']}")

# Hostile keys must not reach docker via the other local branches either.
for name in HOSTILE:
    reset()
    code, resp = act(name, "launch-roblox")
    check(f"hostile key {name!r} + launch-roblox -> 404, zero docker",
          code == 404 and _state["run_calls"] == [],
          f"code={code} runs={_state['run_calls']}")
    reset()
    code, resp = act(name, "roblox-login")
    check(f"hostile key {name!r} + roblox-login -> 404, zero docker",
          code == 404 and _state["run_calls"] == [],
          f"code={code} runs={_state['run_calls']}")

# Unknown-but-valid name keeps the old 404, no docker.
reset()
code, resp = act("rbx99", "start")
check("unknown valid name -> 404, zero docker",
      code == 404 and resp.get("error") == "instance not found"
      and _state["run_calls"] == [],
      f"code={code} runs={_state['run_calls']}")

# Valid local actions: exact docker commands.
reset()
code, resp = act("rbx1", "start")
check("valid start -> 200 with exact docker start",
      code == 200 and resp.get("ok") is True
      and _state["run_calls"] == ["docker start wd-rbx1"],
      f"code={code} resp={resp} runs={_state['run_calls']}")
reset()
code, resp = act("rbx1", "stop")
check("valid stop -> 200 with exact docker stop",
      code == 200 and resp.get("ok") is True
      and _state["run_calls"] == ["docker stop wd-rbx1"],
      f"code={code} resp={resp} runs={_state['run_calls']}")
reset()
code, resp = act("rbx1", "restart")
check("valid restart -> 200 with docker stop then docker start, in order",
      code == 200 and resp.get("ok") is True
      and _state["run_calls"] == ["docker stop wd-rbx1",
                                  "docker start wd-rbx1"],
      f"code={code} resp={resp} runs={_state['run_calls']}")

# Valid remote action still proxies (name reaches the agent URL only
# after passing the gate).
reset()
code, resp = act("srt1", "start")
check("valid remote start -> 200 via wing proxy, zero local docker",
      code == 200 and _state["run_calls"] == []
      and _state["wing_calls"] == [("POST", "/api/agent/instances/srt1/start")],
      f"code={code} resp={resp} runs={_state['run_calls']} "
      f"wing={_state['wing_calls']}")

# Other branches preserved for valid names.
reset()
code, resp = act("rbx1", "launch-roblox")
check("valid launch-roblox -> 200 ok, no docker start/stop",
      code == 200 and resp.get("ok") is True
      and all("docker start" not in c and "docker stop" not in c
              for c in _state["run_calls"]),
      f"code={code} resp={resp} runs={_state['run_calls']}")
reset()
code, resp = act("rbx1", "roblox-login")
check("valid roblox-login -> 200 ok",
      code == 200 and resp.get("ok") is True,
      f"code={code} resp={resp}")
reset()
_state["container"] = "stopped"
code, resp = act("rbx1", "roblox-login")
check("roblox-login on stopped instance -> 409, zero docker",
      code == 409 and _state["run_calls"] == [],
      f"code={code} runs={_state['run_calls']}")
reset()
code, resp = act("rbx1", "bogus-action")
check("unknown action on valid instance -> 400, zero docker",
      code == 400 and resp.get("error") == "unknown action"
      and _state["run_calls"] == [],
      f"code={code} runs={_state['run_calls']}")

# ---------------------------------------------------------------- C. contracts
print("== C. source contracts ==")
src = fn_src("instance_single_action")
check("gate is the first statement of the function (before load_instances)",
      src.index("valid_instance_name(name)") < src.index("load_instances()"))
check("gate precedes the first docker start/stop string",
      src.index("valid_instance_name(name)")
      < src.index("docker {'start' if action == 'start' else 'stop'}"))
check("gate precedes the remote wing_request branch",
      src.index("valid_instance_name(name)") < src.index("wing_request(p,"))
check("gate returns the route-style 404, not a new error shape",
      'return 404, {"error": "instance not found"}' in src)
check("create sink gate kept",
      "valid_instance_name(name)" in fn_src("create_local_instance")
      and "invalid cpus/memory" in fn_src("create_local_instance"))
check("resize sink gate kept",
      "valid_instance_name(name)" in fn_src("resize_local_instance")
      and "invalid cpus/memory" in fn_src("resize_local_instance"))
check("waydroid_exec sink gate kept",
      "if not valid_instance_name(name):" in fn_src("waydroid_exec"))
check("login sink gate kept",
      "if not valid_instance_name(name):" in fn_src("_roblox_login_inner"))
check("start/stop/restart routes still gate too (defence in depth)",
      "instance_single_action(name, m.group(2))" in app_src)
check("remote-autologin app.py snapshot == manager/app.py",
      open(SNAP_APP, encoding="utf-8").read() == app_src)

# ---------------------------------------------------------------- D. negative proof
print("== D. negative proof ==")


def old_action_flow(name, action):
    """Reconstruction of the pre-fix flow: membership check only, then docker."""
    if name not in _state["instances"]:
        return 404, {"error": "instance not found"}
    cname = f"wd-{name}"
    return app.run(f"docker {'start' if action == 'start' else 'stop'} {cname}",
                   timeout=60)


reset()
old_action_flow("rbx1;id>/tmp/p", "start")
check("pre-fix flow DOES reach docker with the hostile command",
      len(_state["run_calls"]) == 1
      and _state["run_calls"][0] == "docker start wd-rbx1;id>/tmp/p",
      f"runs={_state['run_calls']}")

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
