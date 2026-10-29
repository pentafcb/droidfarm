#!/usr/bin/env python3
"""Tests for resize-sink-gate (manager only, no agent change).

GAP (malfunction/security hardening, found by auditing sink-gate
coverage after t162): the manager's resize route validates inputs
(validate_resize_params coerces + bounds memory_mb/cpus) and
resize_local_instance already gates the NAME at the sink — but it did
NOT gate the numerics. memory_mb and cpus go straight into the shell
`docker update` command, and the values are also written verbatim into
instances.json on success. A future internal caller passing a hostile
string (or an uncoerced value, e.g. "2048" stored as a string) would
reach the shell verbatim, even though the route validator is clean
today. The agent side needs no change: its resize route already
coerces + bounds (1024-32768 MB, 0.5-32 cpus) and its docker() is
list-form (no shell).

FIX (manager/app.py only): resize_local_instance coerces + bounds
memory_mb/cpus at the top, before any docker call or state write —
uncoercible or out-of-bounds -> (400, "invalid cpus/memory"),
both-None -> (400, "memory_mb or cpus required"). Bounds match the
route validator exactly (memory 1024-32768 MB, cpus 0.5-32); None
means "keep the existing limit" for that field. The coerced values are
what the docker update, the shrink check, and the state write see,
which is identical for the live caller (it already coerces). No agent,
dashboard, or wire-protocol change.

Covers:
  A. regression proof (contract fact + old shell construction
     interpolates hostile memory/cpus verbatim)
  B. real resize_local_instance (stubbed I/O): invalid-numerics
     matrix -> 400 with zero docker calls and zero state writes;
     both-None -> 400; valid + boundary + string-numeric + partial
     cases -> 200 with exact docker fragments and coerced values
     stored; shrink check still refuses below-usage memory
  C. name gate kept: invalid name still 404s before the numerics
  D. source contracts (gate placement, sibling sink gates kept,
     route validator kept, snapshot parity)
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


app = load("rsg_app", APP)
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
_old_cmd = "docker update --memory 2048;idm --cpus 2;id wd-rbx1"
check("old docker-update construction interpolates hostile memory verbatim",
      "--memory 2048;idm" in _old_cmd)
check("old docker-update construction interpolates hostile cpus verbatim",
      "--cpus 2;id" in _old_cmd)
check("route validator bounds are the sink bounds (1024-32768, 0.5-32)",
      "RESIZE_MIN_MEMORY_MB = 1024" in app_src
      and "RESIZE_MAX_MEMORY_MB = 32768" in app_src
      and "RESIZE_MIN_CPUS = 0.5" in app_src
      and "RESIZE_MAX_CPUS = 32" in app_src)

# ---------------------------------------------------------------- B. real function
print("== B. real resize_local_instance (stubbed I/O) ==")
_state = {"instances": {}, "run_calls": [], "saves": [], "used": None}


def fake_load_instances():
    return dict(_state["instances"])


def fake_save_instances(insts):
    _state["saves"].append(dict(insts))
    _state["instances"] = dict(insts)


def reset(used=None):
    _state["instances"] = {"rbx1": {"memory_mb": 2048, "cpus": 2.0}}
    _state["run_calls"] = []
    _state["saves"] = []
    _state["used"] = used
    app.load_instances = fake_load_instances
    app.save_instances = fake_save_instances
    app.instance_provider_of = lambda rec: "local"
    app.instance_resources = lambda name: (
        {"mem_used_mb": _state["used"]} if _state["used"] is not None else None)
    app.invalidate_instance_snapshot = lambda name: None

    def fake_run(cmd, timeout=None):
        _state["run_calls"].append(cmd)
        return (0, "", "")
    app.run = fake_run


def resize(name, memory_mb, cpus):
    return app.resize_local_instance(name, memory_mb, cpus)


# (memory_mb, cpus) pairs; None in a slot means "keep existing" and is
# valid on its own, so invalid-None cases are covered by both-None below.
BAD_NUMERICS = [
    ("2048;id", 2.0), ("abc", 2.0), ("2048.5", 2.0),
    (512, 2.0), (1023, 2.0), (32769, 2.0), (float("inf"), 2.0),
    (True, 2.0),  # int(True) == 1 < 1024
    (2048, "2;id"), (2048, "abc"), (2048, 0.1), (2048, 0.49),
    (2048, 32.1), (2048, 100), (2048, float("nan")),
    (2048, float("inf")), (2048, [2]), (2048, {"x": 1}),
]
for mem, cpus in BAD_NUMERICS:
    reset()
    code, resp = resize("rbx1", mem, cpus)
    check(f"invalid numerics mem={mem!r} cpus={cpus!r} -> 400, "
          "zero docker, zero writes",
          code == 400 and resp.get("error") == "invalid cpus/memory"
          and _state["run_calls"] == [] and _state["saves"] == [],
          f"code={code} resp={resp} runs={_state['run_calls']} "
          f"saves={len(_state['saves'])}")

reset()
code, resp = resize("rbx1", None, None)
check("both-None -> 400, zero docker, zero writes",
      code == 400 and resp.get("error") == "memory_mb or cpus required"
      and _state["run_calls"] == [] and _state["saves"] == [],
      f"code={code} resp={resp}")

# Valid: exact docker fragments, coerced values stored.
reset()
code, resp = resize("rbx1", 4096, 4.0)
check("valid (4096, 4.0) -> 200 with exact docker fragments",
      code == 200 and resp.get("ok") is True
      and len(_state["run_calls"]) == 1
      and "docker update --memory 4096m --cpus 4.0 wd-rbx1"
      in _state["run_calls"][0]
      and _state["instances"]["rbx1"]["memory_mb"] == 4096
      and _state["instances"]["rbx1"]["cpus"] == 4.0
      and isinstance(_state["instances"]["rbx1"]["memory_mb"], int)
      and isinstance(_state["instances"]["rbx1"]["cpus"], float),
      f"code={code} resp={resp} runs={_state['run_calls']} "
      f"inst={_state['instances'].get('rbx1')}")

# String numerics are coerced (same as the route validator).
reset()
code, resp = resize("rbx1", "4096", "4")
check("string numerics coerced and stored as int/float",
      code == 200 and "--memory 4096m" in _state["run_calls"][0]
      and "--cpus 4.0" in _state["run_calls"][0]
      and isinstance(_state["instances"]["rbx1"]["memory_mb"], int)
      and isinstance(_state["instances"]["rbx1"]["cpus"], float),
      f"code={code} runs={_state['run_calls']} "
      f"inst={_state['instances'].get('rbx1')}")

# Partial: only memory — docker carries only --memory, cpus preserved.
reset()
code, resp = resize("rbx1", 8192, None)
check("memory-only resize: only --memory in docker, cpus preserved",
      code == 200 and "--memory 8192m" in _state["run_calls"][0]
      and "--cpus" not in _state["run_calls"][0]
      and _state["instances"]["rbx1"]["cpus"] == 2.0
      and resp.get("memory_mb") == 8192 and resp.get("cpus") == 2.0,
      f"code={code} resp={resp} runs={_state['run_calls']}")

# Partial: only cpus — docker carries only --cpus, memory preserved.
reset()
code, resp = resize("rbx1", None, 1.5)
check("cpus-only resize: only --cpus in docker, memory preserved",
      code == 200 and "--cpus 1.5" in _state["run_calls"][0]
      and "--memory" not in _state["run_calls"][0]
      and _state["instances"]["rbx1"]["memory_mb"] == 2048
      and resp.get("memory_mb") == 2048 and resp.get("cpus") == 1.5,
      f"code={code} resp={resp} runs={_state['run_calls']}")

# Boundaries, inclusive on both ends.
for mem, cpus in [(1024, 0.5), (32768, 32), (2048, 2.5)]:
    reset()
    code, resp = resize("rbx1", mem, cpus)
    check(f"boundary valid ({mem}, {cpus}) -> 200",
          code == 200 and resp.get("ok") is True
          and len(_state["run_calls"]) == 1,
          f"code={code} resp={resp}")

# Shrink check still refuses below-usage memory, before docker.
reset(used=3000)
code, resp = resize("rbx1", 2048, None)
check("shrink below usage still 400s, zero docker, zero writes",
      code == 400 and "below current usage" in str(resp.get("error", ""))
      and _state["run_calls"] == [] and _state["saves"] == [],
      f"code={code} resp={resp}")

# ---------------------------------------------------------------- C. name gate kept
print("== C. name gate kept ==")
for name in ["", "../x", "a/b", "x;y", "a" * 33]:
    reset()
    code, resp = resize(name, 4096, 4.0)
    check(f"invalid name {name!r} -> 404 before numerics, zero docker",
          code == 404 and resp.get("error") == "instance not found"
          and _state["run_calls"] == [] and _state["saves"] == [],
          f"code={code} resp={resp}")

# ---------------------------------------------------------------- D. contracts
print("== D. source contracts ==")
src = fn_src("resize_local_instance")
check("gate precedes docker update string",
      src.index("invalid cpus/memory")
      < src.index("docker update {' '.join(args)}"))
check("gate precedes first with _lock:",
      src.index("invalid cpus/memory") < src.index("with _lock:"))
check("gate uses the RESIZE_* constants, not literals",
      "RESIZE_MIN_MEMORY_MB" in src and "RESIZE_MAX_MEMORY_MB" in src
      and "RESIZE_MIN_CPUS" in src and "RESIZE_MAX_CPUS" in src)
check("name gate still first in the function",
      src.index("valid_instance_name(name)") < src.index("invalid cpus/memory"))
check("create sink gate kept",
      "valid_instance_name(name)" in fn_src("create_local_instance")
      and "invalid cpus/memory" in fn_src("create_local_instance"))
check("waydroid_exec sink gate kept",
      "if not valid_instance_name(name):" in fn_src("waydroid_exec"))
check("resize route validator kept",
      "RESIZE_MIN_MEMORY_MB <= memory_mb <= RESIZE_MAX_MEMORY_MB"
      in fn_src("validate_resize_params"))
check("agent resize route still coerces + bounds",
      "1024 <= memory_mb <= 32768" in open(
          os.path.join(REPO, "wing", "agent.py"), encoding="utf-8").read())
check("remote-autologin app.py snapshot == manager/app.py",
      open(SNAP_APP, encoding="utf-8").read() == app_src)

# ---------------------------------------------------------------- E. negative proof
print("== E. negative proof ==")


def old_resize_flow(name, mem, cpus):
    """Reconstruction of the pre-fix flow: no numeric gate, straight to docker."""
    args = []
    if mem is not None:
        args.append(f"--memory {mem}m")
    if cpus is not None:
        args.append(f"--cpus {cpus}")
    return app.run(f"docker update {' '.join(args)} wd-{name}", timeout=60)


reset()
old_resize_flow("rbx1", "2048;id", "2;id")
check("pre-fix flow DOES reach docker with the hostile command",
      len(_state["run_calls"]) == 1
      and "--memory 2048;idm" in _state["run_calls"][0]
      and "--cpus 2;id" in _state["run_calls"][0],
      f"runs={_state['run_calls']}")

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
