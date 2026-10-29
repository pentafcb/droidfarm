#!/usr/bin/env python3
"""Tests for create-sink-gate (manager only, no agent change).

GAP (malfunction/security hardening, found by auditing sink-gate
coverage after t161): the manager's HTTP routes all validate create
inputs (the single-create route coerces + bounds cpus/memory_mb, the
fleet worker goes through _fleet_validate, the name is stripped or
generated) and waydroid_exec / docker_inspect gate names at the sink —
but create_local_instance itself, the shared create core, did NOT.
Its name, cpus and memory_mb go straight into the shell `docker run`
command, and the ip gate added by create-ip-gate covers only the
fourth interpolation. docker_inspect's gate does NOT stop a hostile
name here: it only makes the name look "missing", and the create then
proceeds to reserve, pass the ip gate, and run docker with the raw
name. The agent side needs no change: its docker() is list-form (no
shell), and create_instance_linux already fullmatch-rejects the name
and the ip.

FIX (manager/app.py only): create_local_instance fails closed at the
top of the function, before any reservation, docker call, or state
write — invalid name -> (404, "instance not found"), uncoercible or
out-of-bounds cpus/memory_mb -> (400, "invalid cpus/memory"). Bounds
match the route/fleet validators exactly (cpus 0.5-32, memory
1024-32768 MB); the coerced values are what the reservation and the
docker run see, which is identical for both live callers (they already
coerce). No agent, dashboard, or wire-protocol change.

Covers:
  A. regression proof (contract fact + old shell construction
     interpolates hostile name/cpus/memory verbatim)
  B. real create_local_instance (stubbed loads/run/docker helpers):
     invalid-name matrix -> 404 with zero run calls and zero state
     writes; invalid-numerics matrix -> 400 with zero run calls and
     zero state writes; valid + boundary + string-numeric cases ->
     200 with the exact docker run fragments and coerced values
     stored in the reservation
  C. ip gate unaffected: hostile ip still 400s after the new gate
  D. source contracts (gate placement + indent, ip gate kept,
     sibling sink gates kept, route coercion kept, snapshot parity)
  E. negative proof (reconstructed pre-fix flow DOES reach docker
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


app = load("csg_app", APP)
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

# ---------------------------------------------------------------- A. regression
print("== A. regression proof (contract + shell-construction facts) ==")
check("every attack-shaped name fails the gate contract",
      all(not app.valid_instance_name(n) for n in BAD_NAMES)
      and app.valid_instance_name("rbx1"),
      f"matrix={[app.valid_instance_name(n) for n in BAD_NAMES]}")
# The pre-fix construction glued the raw inputs into one shell string.
_old_cmd = ("docker run -d --name wd-../../tmp/evil --privileged "
            "--network droid-net --ip 10.20.0.99 "
            "--cpus 2;id --memory 2048;idm test-image")
check("old docker-run construction interpolates hostile name verbatim",
      "wd-../../tmp/evil" in _old_cmd)
check("old docker-run construction interpolates hostile cpus verbatim",
      "--cpus 2;id" in _old_cmd)
check("old docker-run construction interpolates hostile memory verbatim",
      "--memory 2048;idm" in _old_cmd)

# ---------------------------------------------------------------- B. real function
print("== B. real create_local_instance (stubbed I/O) ==")
_state = {"instances": {}, "run_calls": [], "saves": []}


def fake_load_instances():
    return dict(_state["instances"])


def fake_save_instances(insts):
    _state["saves"].append(dict(insts))
    _state["instances"] = dict(insts)


def reset():
    _state["instances"] = {}
    _state["run_calls"] = []
    _state["saves"] = []
    app.load_instances = fake_load_instances
    app.save_instances = fake_save_instances
    app.docker_inspect = lambda cname: None
    app.next_free_ip = lambda insts: "10.20.0.99"
    app.free_binder_triplet = lambda used: ("binder", "binder1", "binder2")
    app.used_binders = lambda insts: set()
    app.ensure_binder_devices = lambda: True
    app.waydroid_image_present = lambda: True
    app.ensure_loop_devices = lambda: True
    app.count_loop_devices = lambda: 64
    app.next_instance_id = lambda insts: 5

    def fake_run(cmd, timeout=None):
        _state["run_calls"].append(cmd)
        return (0, "", "")
    app.run = fake_run


def create(name, cpus, memory_mb, ip=None):
    return app.create_local_instance(name, cpus, memory_mb, ip=ip)


for name in BAD_NAMES:
    reset()
    code, resp = create(name, 2.0, 2048)
    check(f"invalid name {name!r} -> 404, zero docker, zero writes",
          code == 404 and resp.get("error") == "instance not found"
          and _state["run_calls"] == [] and _state["saves"] == [],
          f"code={code} resp={resp} runs={_state['run_calls']} "
          f"saves={len(_state['saves'])}")

BAD_NUMERICS = [
    ("2;id", 2048), ("$(id)", 2048), ("abc", 2048), (None, 2048),
    (0.1, 2048), (0.49, 2048), (32.1, 2048), (100, 2048),
    (float("nan"), 2048), (float("inf"), 2048),
    (2.0, "2048;id"), (2.0, "abc"), (2.0, None), (2.0, 512),
    (2.0, 1023), (2.0, 32769), (2.0, float("inf")), (2.0, True),
]
for cpus, mem in BAD_NUMERICS:
    reset()
    code, resp = create("rbx1", cpus, mem)
    check(f"invalid numerics cpus={cpus!r} mem={mem!r} -> 400, "
          "zero docker, zero writes",
          code == 400 and resp.get("error") == "invalid cpus/memory"
          and _state["run_calls"] == [] and _state["saves"] == [],
          f"code={code} resp={resp} runs={_state['run_calls']} "
          f"saves={len(_state['saves'])}")

# Valid: floats/ints, generated ip, exact docker fragments, stored values.
reset()
code, resp = create("rbx1", 2.0, 2048)
check("valid (2.0, 2048) -> 200 with exact docker fragments",
      code == 200 and resp.get("ok") is True
      and len(_state["run_calls"]) == 1
      and "--name wd-rbx1" in _state["run_calls"][0]
      and "--cpus 2.0" in _state["run_calls"][0]
      and "--memory 2048m" in _state["run_calls"][0]
      and "--ip 10.20.0.99" in _state["run_calls"][0]
      and _state["instances"]["rbx1"]["cpus"] == 2.0
      and _state["instances"]["rbx1"]["memory_mb"] == 2048,
      f"code={code} resp={resp} runs={_state['run_calls']} "
      f"inst={_state['instances'].get('rbx1')}")

# Valid: string numerics are coerced (same as the route), explicit ip kept.
reset()
code, resp = create("rbx2", "2", "2048", ip="10.20.0.50")
check("string numerics coerced, explicit ip kept",
      code == 200 and "--cpus 2.0" in _state["run_calls"][0]
      and "--memory 2048m" in _state["run_calls"][0]
      and "--ip 10.20.0.50" in _state["run_calls"][0]
      and isinstance(_state["instances"]["rbx2"]["cpus"], float)
      and isinstance(_state["instances"]["rbx2"]["memory_mb"], int),
      f"code={code} runs={_state['run_calls']} "
      f"inst={_state['instances'].get('rbx2')}")

# Boundaries, inclusive on both ends.
for name, cpus, mem in [("rbx3", 0.5, 1024), ("rbx4", 32, 32768),
                        ("rbx5", 2.5, 4096)]:
    reset()
    code, resp = create(name, cpus, mem)
    check(f"boundary valid ({cpus}, {mem}) -> 200",
          code == 200 and resp.get("ok") is True
          and len(_state["run_calls"]) == 1,
          f"code={code} resp={resp}")

# ---------------------------------------------------------------- C. ip gate kept
print("== C. ip gate unaffected by the new gate ==")
reset()
code, resp = create("rbx1", 2.0, 2048, ip="10.20.0.5;touch${IFS}/tmp/x")
check("hostile ip still 400s after the new gate, no docker, rolled back",
      code == 400 and "invalid IP" in str(resp.get("error", ""))
      and _state["run_calls"] == []
      and "rbx1" not in _state["instances"],
      f"code={code} resp={resp} runs={_state['run_calls']}")

# ---------------------------------------------------------------- D. contracts
print("== D. source contracts ==")
src = fn_src("create_local_instance")
check("gate precedes reservation",
      src.index("valid_instance_name(name)")
      < src.index("instances[name] = {"))
check("gate precedes docker run string",
      src.index("valid_instance_name(name)")
      < src.index("docker run -d"))
glines = src.splitlines()
gidx = next(i for i, l in enumerate(glines)
             if "valid_instance_name(name)" in l)
check("gate at function-body indent (before any with _lock:)",
      glines[gidx].startswith("    if ")
      and not any(l.startswith("        with _lock:")
                  for l in glines[:gidx]))
check("ip gate still present in the same function",
      "valid_ipv4(ip)" in src)
check("waydroid_exec sink gate kept",
      "if not valid_instance_name(name):" in fn_src("waydroid_exec"))
check("docker_inspect sink gate kept",
      're.fullmatch(r"wd-[A-Za-z0-9_-]{1,32}"' in fn_src("docker_inspect"))
check("single-create route coercion + bounds kept",
      'cpus = float(data.get("cpus", 2))' in app_src
      and 'memory_mb = int(data.get("memory_mb", 4096))' in app_src)
check("remote-autologin app.py snapshot == manager/app.py",
      open(SNAP_APP, encoding="utf-8").read() == app_src)

# ---------------------------------------------------------------- E. negative proof
print("== E. negative proof ==")


def old_create_flow(name, cpus, mem):
    """Reconstruction of the pre-fix flow: no gate, straight to docker."""
    cname = f"wd-{name}"
    cmd = (f"docker run -d --name {cname} --privileged "
           f"--cpus {cpus} --memory {mem}m test-image")
    return app.run(cmd, timeout=60)


reset()
old_create_flow("../../tmp/evil", "2;id", "2048;id")
check("pre-fix flow DOES reach docker with the hostile command",
      len(_state["run_calls"]) == 1
      and "wd-../../tmp/evil" in _state["run_calls"][0]
      and "--cpus 2;id" in _state["run_calls"][0],
      f"runs={_state['run_calls']}")

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
