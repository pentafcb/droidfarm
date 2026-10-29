#!/usr/bin/env python3
"""Tests for the agent create-numerics sink gate (agent-create-numerics-sink-gate).

Census finding (2026-10-28): the agent-side sink census (t170) closed the
NAME sinks, and the t170 session named numerics as the adjacent class —
`create_instance_linux()` took cpus/memory_mb straight into the docker run
argv and the state record. Its route clamps today (cpus 1-16, memory
512-32768), but the sink itself validated nothing: a future internal
caller passing an uncoercible, negative, fractional-memory, or absurd
value would drive docker/state before any route gate ever ran. (List-form
docker argv means no shell injection here — the failure mode is absurd
limits and corrupted state, e.g. memory 999999999 or cpus "2;rm" stored
verbatim.)

Fix (wing/agent.py only, no manager/dashboard/wire change, no VERSION
bump — the manager's agent gate is >= 1.0.2, and bumping would churn 53
test files' VERSION assertions for zero behavioral gain): the sink
coerces (cpus float, memory int, bools rejected, fractional memory
rejected) and bounds-checks against the route's clamp output exactly
(cpus 1-16, memory 512-32768 MB), failing closed with
(False, "invalid cpus/memory") before any docker/state touch. Integral
cpus normalize back to int so the valid-path argv and state record are
byte-identical to today. The tighter manager-side create policy
(0.5-32 / 1024+) stays enforced upstream.

Covers:
  A. regression proof (old flow embeds raw numerics verbatim in argv/state)
  B. hostile numerics matrix -> refused, zero docker calls, zero saves
  C. valid paths unchanged (exact argv, coerced state, boundaries)
  D. gate placement + sibling gates kept + route clamp kept
  E. deploy wiring (agent.py staged, VERSION unchanged at 1.0.45)
  F. negative proof (pre-fix argv reaches docker verbatim)
"""
import importlib.util
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
DEPLOY_ALL = os.path.join(REPO, "pending-deploy", "apply-all", "deploy-all.sh")

PASS = FAIL = 0


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


agent = load("acnsg_agent", AGENT)

NAN = float("nan")
INF = float("inf")

# --- A. regression proof: the pre-fix flow embeds raw numerics verbatim --
old_argv = ["run", "-d", "--cpus", str("2;rm"), "--memory", "999999999m"]
check("2;rm" in old_argv and "999999999m" in old_argv,
      "regression: old argv embeds hostile numerics verbatim", repr(old_argv))
old_state = {"cpus": "2;rm", "memory_mb": 999999999}
check(old_state["cpus"] == "2;rm" and old_state["memory_mb"] == 999999999,
      "regression: old state stores hostile numerics verbatim", repr(old_state))
check(str(-1) == "-1" and str(0.5) == "0.5",
      "regression: old flow passes negative/fractional cpus through", "")


class FakeDocker:
    """Stand-in for agent.docker: records calls, simulates an empty node."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[0] == "network":
            return 0, agent.NETWORK
        if args[0] == "run":
            return 0, "deadbeef"
        return 0, ""


def setup():
    fd = FakeDocker()
    saved = []
    agent.docker = fd
    agent.container_status = lambda cname: "missing"
    agent.sw_image_present = lambda: True
    agent.sw_ensure_loop_devices = lambda: True
    agent.sw_find_binder_triplet = lambda: ["/dev/binderfs/binder",
                                            "/dev/binderfs/vndbinder",
                                            "/dev/binderfs/hwbinder"]
    agent.load_instances = lambda: {}
    agent.save_instances = lambda data: saved.append(dict(data))
    return fd, saved


# --- B. hostile numerics matrix --------------------------------------------
HOSTILE_CPUS = [None, True, False, "abc", "2;rm", "", NAN, INF, -INF,
                0, -1, 0.5, 0.99, 17, 100, 10 ** 9]
for h in HOSTILE_CPUS:
    fd, saved = setup()
    try:
        out = agent.create_instance_linux("rbx1", h, 2048, "10.20.0.11")
    except Exception as e:
        bad(f"create hostile cpus {h!r} raises", repr(e))
        continue
    check(out == (False, "invalid cpus/memory"),
          f"create hostile cpus {h!r} -> refused", repr(out))
    check(fd.calls == [] and saved == [],
          f"create hostile cpus {h!r} -> zero docker/state", repr(fd.calls))

HOSTILE_MEM = [None, True, False, "abc", "2048;rm", "", 2048.5, "2048.5",
               NAN, INF, -INF, 0, -512, 511, 32769, 10 ** 9]
for h in HOSTILE_MEM:
    fd, saved = setup()
    try:
        out = agent.create_instance_linux("rbx1", 2, h, "10.20.0.11")
    except Exception as e:
        bad(f"create hostile memory {h!r} raises", repr(e))
        continue
    check(out == (False, "invalid cpus/memory"),
          f"create hostile memory {h!r} -> refused", repr(out))
    check(fd.calls == [] and saved == [],
          f"create hostile memory {h!r} -> zero docker/state", repr(fd.calls))

# --- C. valid paths unchanged ------------------------------------------------
fd, saved = setup()
out = agent.create_instance_linux("rbx1", 2, 2048, "10.20.0.11")
check(out == (True, "created"), "create valid (2, 2048) -> created", repr(out))
run = [c for c in fd.calls if c[0] == "run"]
check(len(run) == 1 and "--cpus" in run[0] and run[0][run[0].index("--cpus") + 1] == "2",
      "create valid -> argv cpus exactly '2'", repr(run))
check(len(run) == 1 and run[0][run[0].index("--memory") + 1] == "2048m",
      "create valid -> argv memory exactly '2048m'", repr(run))
check(len(saved) == 1 and saved[0]["rbx1"]["cpus"] == 2
      and type(saved[0]["rbx1"]["cpus"]) is int,
      "create valid -> state cpus int 2", repr(saved))
check(len(saved) == 1 and saved[0]["rbx1"]["memory_mb"] == 2048,
      "create valid -> state memory 2048", repr(saved))

fd, saved = setup()
out = agent.create_instance_linux("rbx1", "2", "2048", "10.20.0.11")
check(out == (True, "created"), "create string numerics -> created", repr(out))
check(len(saved) == 1 and saved[0]["rbx1"]["cpus"] == 2
      and saved[0]["rbx1"]["memory_mb"] == 2048,
      "create string numerics -> coerced state", repr(saved))

fd, saved = setup()
out = agent.create_instance_linux("rbx1", 2.5, 2048, "10.20.0.11")
check(out == (True, "created"), "create fractional cpus 2.5 -> created", repr(out))
run = [c for c in fd.calls if c[0] == "run"]
check(len(run) == 1 and run[0][run[0].index("--cpus") + 1] == "2.5",
      "create fractional cpus -> argv '2.5'", repr(run))
check(len(saved) == 1 and saved[0]["rbx1"]["cpus"] == 2.5,
      "create fractional cpus -> state 2.5", repr(saved))

fd, saved = setup()
out = agent.create_instance_linux("rbx1", 1, 512, "10.20.0.11")
check(out == (True, "created"), "create lower boundary (1, 512) -> created", repr(out))
fd, saved = setup()
out = agent.create_instance_linux("rbx1", 16, 32768, "10.20.0.11")
check(out == (True, "created"), "create upper boundary (16, 32768) -> created", repr(out))

# --- D. gate placement + sibling gates kept ----------------------------------
src = inspect.getsource(agent.create_instance_linux)
check(src.index("agent-create-numerics-sink-gate") < src.index("container_status(cname)"),
      "gate placement: numerics gate precedes first docker touch", "")
check(src.index("invalid cpus/memory") < src.index("valid_agent_ipv4(ip)"),
      "gate placement: numerics gate precedes ip gate + run", "")
check('re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name)' in src,
      "sibling kept: name gate in create sink", "")
check("valid_agent_ipv4(ip)" in src, "sibling kept: ip gate in create sink", "")
check("max(1, min(16, int(cpus)))" in open(AGENT, encoding="utf-8").read(),
      "route clamp kept: create route still clamps cpus 1-16", "")
check("valid_agent_instance_name(name)" in inspect.getsource(agent.instance_status_linux),
      "sibling kept: status sink name gate", "")
check("valid_agent_instance_name(name)" in inspect.getsource(agent.delete_instance_linux),
      "sibling kept: delete sink name gate", "")

# --- E. deploy wiring ----------------------------------------------------------
deploy = open(DEPLOY_ALL, encoding="utf-8").read()
check("wing/agent.py" in deploy,
      "deploy wiring: agent.py already in apply-all stage list")
check(agent.VERSION == "1.0.45",
      "VERSION deliberately unchanged at 1.0.45 (no bump needed)",
      repr(agent.VERSION))

# --- F. negative proof ---------------------------------------------------------
pre_argv = ["run", "-d", "--name", "wd-rbx1", "--cpus", str("0;rm"),
            "--memory", f"{10 ** 9}m"]
fd, saved = setup()
agent.docker = lambda *a, timeout=60: (fd.calls.append(a), (0, ""))[1]
agent.docker("run", *pre_argv[1:])
check(any("0;rm" in c for c in fd.calls),
      "negative proof: pre-fix argv reaches docker verbatim", repr(fd.calls))

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
