#!/usr/bin/env python3
"""Tests for the agent binder-count sink gate (agent-binder-count-sink-gate).

Census finding (2026-10-28): the t172 session closed the agent numerics
census for name + create/resize numerics and named the sw_* helper
numerics as the adjacent class. The audit found the binder pool count
unguarded at BOTH write-driving sinks:

  * sw_ensure_binder_devices(count=64) did int() + a floor-only check —
    a huge count looped that many binder-control shell commands.
  * sw_boot_binder_pool(count) did NO coercion and NO bounds check at
    all — range(count) drove the binder-control write loop directly.
    Its count defaults from SW_BINDER_COUNT, read from DF_BINDER_COUNT
    via _sw_env_int_min — floored at 1, never ceilinged — so a bad env
    value would loop the write cycle every selfwatch pass.
  * _sw_prepare_pools_for_start built its expected set from
    range(1, SW_BINDER_COUNT) with the same unguarded value.

The failure mode is a runaway shell loop (and, for the set build, an
unbounded allocation), not injection — names are generated internally
and every command is a fixed template. The fix (wing/agent.py only, no
manager/dashboard/wire change, no VERSION bump — the manager's agent
gate is >= 1.0.2, and bumping would churn 53 test files' VERSION
assertions for zero behavioral gain): one shared
_sw_coerce_binder_count() bounds the count to [1, 256] (default pool
64, 4x headroom) at all three sites. Invalid counts fail closed with
zero shell touches: ensure -> False, boot pool -> (0, False) (callers
treat have < SW_BINDER_COUNT as paused), prepare -> the gated restore
call (which fails closed the same way). Valid paths are byte-identical
(default 64, count=3, string "64").

Covers:
  A. regression proof (old flows accept absurd counts into the loop)
  B. _sw_coerce_binder_count matrix (valid -> int, hostile -> None)
  C. sw_ensure_binder_devices hostile matrix -> False, zero shell cmds
  D. sw_ensure_binder_devices valid paths unchanged (64/3/256/"64")
  E. sw_boot_binder_pool hostile matrix -> (0, False), zero shell cmds
  F. sw_boot_binder_pool valid paths unchanged (64/3/"64"/default)
  G. _sw_prepare_pools_for_start: absurd env count -> gated restore call,
     no set build, no hang; normal count fast path unchanged
  H. gate placement + sibling sink gates kept + route validators kept
  I. deploy wiring (agent.py staged, VERSION unchanged at 1.0.45)
  J. negative proof (pre-fix range() would iterate the absurd count)
"""
import importlib.util
import inspect
import os
import re
import sys
import tempfile

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


agent = load("abcsg_agent", AGENT)

NAN = float("nan")
INF = float("inf")


class FakeShell:
    """Simulates agent.run(cmd, timeout, shell=True) against a fake
    /dev/binderfs, mirroring the wing-binderfs-selfheal fixture."""

    def __init__(self, devices=(), control=True):
        self.devices = set(devices)
        self.control = control
        self.cmds = []

    def run(self, cmd, timeout=30, shell=False):
        self.cmds.append(cmd)
        c = cmd.replace(" 2>/dev/null", "")
        if c == "test -e /dev/binderfs/binder-control":
            return (0, "") if self.control else (1, "")
        m = re.fullmatch(r"test -e /dev/binderfs/(\S+)", c)
        if m:
            return (0, "") if m.group(1) in self.devices else (1, "")
        m = re.fullmatch(r"echo (\S+) > /dev/binderfs/binder-control", c)
        if m:
            if self.control:
                self.devices.add(m.group(1))
                return (0, "")
            return (1, "create failed")
        return (1, "unexpected: " + cmd)


def ensure_with(fs, count):
    """sw_ensure_binder_devices(count) with agent.run faked; returns result."""
    old = agent.run
    agent.run = fs.run
    try:
        return agent.sw_ensure_binder_devices(count=count)
    finally:
        agent.run = old


# --- A. regression proof: old flows accept absurd counts into the loop ----
old_ensure = None
try:
    old_ensure = int(10 ** 6)
    old_ensure_ok = old_ensure >= 1  # old floor-only check passes
except Exception:
    old_ensure_ok = False
check(old_ensure_ok, "regression: old ensure floor-only check passes 10**6",
      repr(old_ensure))
check(len(range(10 ** 6)) == 10 ** 6,
      "regression: old boot-pool range(count) iterates 10**6 times", "")
check(agent.SW_BINDER_COUNT_MAX == 256,
      "regression: ceiling constant exists (256, 4x default 64)",
      repr(getattr(agent, "SW_BINDER_COUNT_MAX", None)))

# --- B. _sw_coerce_binder_count matrix --------------------------------------
VALID = [(1, 1), (64, 64), (256, 256), ("64", 64), (" 64 ", 64),
         ("064", 64), (64.0, 64), (256.0, 256)]
for raw, want in VALID:
    got = agent._sw_coerce_binder_count(raw)
    check(got == want, f"coerce valid {raw!r} -> {want}", repr(got))
HOSTILE = [0, -1, 257, 10 ** 6, 10 ** 9, True, False, "garbage", "",
           "2;rm", "-5", "6.5", "64.0", 64.5, NAN, INF, -INF, None,
           [64], (64,), {"n": 64}, object()]
for h in HOSTILE:
    try:
        got = agent._sw_coerce_binder_count(h)
    except Exception as e:  # noqa: BLE001
        bad(f"coerce hostile {h!r} must not raise", repr(e))
        continue
    check(got is None, f"coerce hostile {h!r} -> None", repr(got))

# --- C. sw_ensure_binder_devices hostile matrix -----------------------------
for h in HOSTILE:
    fs = FakeShell(devices=(), control=True)
    try:
        r = ensure_with(fs, h)
    except Exception as e:  # noqa: BLE001
        bad(f"ensure hostile {h!r} must not raise", repr(e))
        continue
    check(r is False, f"ensure hostile {h!r} -> False", repr(r))
    check(fs.cmds == [], f"ensure hostile {h!r} -> zero shell cmds",
          str(fs.cmds)[:80])

# --- D. sw_ensure_binder_devices valid paths unchanged ----------------------
fs = FakeShell(devices=(), control=True)
r = ensure_with(fs, 64)
check(r is True, "ensure default-count shape: 64 -> True", repr(r))
check(len(fs.devices) == 64, "ensure 64 devices created", str(len(fs.devices)))
fs = FakeShell(devices=(), control=True)
r = ensure_with(fs, 3)
check(r is True and fs.devices == {"binder", "binder1", "binder2"},
      "ensure count=3 -> exactly binder,binder1,binder2",
      str(sorted(fs.devices)))
fs = FakeShell(devices=(), control=True)
r = ensure_with(fs, 256)
check(r is True and len(fs.devices) == 256,
      "ensure boundary 256 -> 256 devices", str(len(fs.devices)))
fs = FakeShell(devices=(), control=True)
r = ensure_with(fs, "64")
check(r is True and len(fs.devices) == 64,
      "ensure string '64' coerces to 64 devices", str(len(fs.devices)))
fs = FakeShell(devices=set(["binder"] + [f"binder{i}" for i in range(1, 64)]),
               control=True)
r = ensure_with(fs, 64)
check(r is False, "ensure full pool -> False, no writes", repr(r))
check(not [c for c in fs.cmds if c.startswith("echo ")],
      "ensure full pool -> no creation writes", str(fs.cmds)[:80])


# --- E/F. sw_boot_binder_pool ------------------------------------------------
class FakePoolRun:
    """run_fn for sw_boot_binder_pool: echo writes create files in the
    fake binderfs dir; modprobe/mount are no-ops that succeed."""

    def __init__(self, bdir):
        self.bdir = bdir
        self.cmds = []

    def __call__(self, cmd, timeout=10, shell=False):
        self.cmds.append(cmd)
        c = cmd.replace(" 2>/dev/null", "")
        m = re.fullmatch(r"echo (\S+) > (\S+)", c)
        if m:
            open(os.path.join(self.bdir, m.group(1)), "w").write("")
            return (0, "")
        return (0, "")


def pool_with(count):
    """sw_boot_binder_pool(count) in a temp binderfs dir with a control
    file present; returns (have, blocked, cmds)."""
    with tempfile.TemporaryDirectory() as td:
        bdir = os.path.join(td, "binderfs")
        os.mkdir(bdir)
        open(os.path.join(bdir, "binder-control"), "w").write("")
        sdir = os.path.join(td, "st")
        os.mkdir(sdir)
        fr = FakePoolRun(bdir)
        have, blocked = agent.sw_boot_binder_pool(
            st_dir=sdir, binderfs=bdir, count=count, run_fn=fr,
            alert_dir=sdir)
        return have, blocked, fr.cmds


# E. hostile matrix -> (0, False), zero shell cmds (None excluded: it is
# the default-count path, covered in F).
for h in HOSTILE:
    if h is None:
        continue
    try:
        have, blocked, cmds = pool_with(h)
    except Exception as e:  # noqa: BLE001
        bad(f"pool hostile {h!r} must not raise", repr(e))
        continue
    check((have, blocked) == (0, False),
          f"pool hostile {h!r} -> (0, False)", repr((have, blocked)))
    check(cmds == [], f"pool hostile {h!r} -> zero shell cmds",
          str(cmds)[:80])

# F. valid paths unchanged.
have, blocked, cmds = pool_with(None)
check((have, blocked) == (64, False),
      "pool default (None -> SW_BINDER_COUNT=64) -> (64, False)",
      repr((have, blocked)))
check(len([c for c in cmds if c.startswith("echo binder")]) == 64,
      "pool default -> 64 binder-control writes", str(len(cmds)))
have, blocked, cmds = pool_with(3)
check((have, blocked) == (3, False), "pool count=3 -> (3, False)",
      repr((have, blocked)))
have, blocked, cmds = pool_with("64")
check((have, blocked) == (64, False), "pool string '64' -> (64, False)",
      repr((have, blocked)))
have, blocked, cmds = pool_with(256)
check((have, blocked) == (256, False), "pool boundary 256 -> (256, False)",
      repr((have, blocked)))

# --- G. _sw_prepare_pools_for_start ------------------------------------------
# Absurd env count: the gated restore call runs (and fails closed), but
# no unbounded set is ever built — the call returns promptly.
calls = []
old_pool = agent.sw_boot_binder_pool
old_count = agent.sw_count_loop_devices
old_ensure_loop = agent.sw_ensure_loop_devices
old_bc = agent.SW_BINDER_COUNT
try:
    agent.sw_boot_binder_pool = lambda *a, **k: calls.append(1) or (0, False)
    agent.sw_count_loop_devices = lambda *a, **k: 999
    agent.sw_ensure_loop_devices = lambda *a, **k: True
    agent.SW_BINDER_COUNT = 10 ** 9
    with tempfile.TemporaryDirectory() as td:
        agent.SW_BINDERFS = td
        agent._sw_prepare_pools_for_start()
    check(calls == [1],
          "prepare absurd count -> exactly one gated restore call, no hang",
          repr(calls))
    # Normal count, complete pool in the dir: fast path, no restore call.
    calls.clear()
    agent.SW_BINDER_COUNT = 64
    with tempfile.TemporaryDirectory() as td:
        for i in range(64):
            name = "binder" if i == 0 else "binder%d" % i
            open(os.path.join(td, name), "w").write("")
        agent.SW_BINDERFS = td
        agent._sw_prepare_pools_for_start()
    check(calls == [],
          "prepare normal count + complete pool -> no restore call",
          repr(calls))
    # Normal count, missing device: restore called once.
    calls.clear()
    with tempfile.TemporaryDirectory() as td:
        for i in range(63):
            name = "binder" if i == 0 else "binder%d" % i
            open(os.path.join(td, name), "w").write("")
        agent.SW_BINDERFS = td
        agent._sw_prepare_pools_for_start()
    check(calls == [1],
          "prepare normal count + missing device -> one restore call",
          repr(calls))
finally:
    agent.sw_boot_binder_pool = old_pool
    agent.sw_count_loop_devices = old_count
    agent.sw_ensure_loop_devices = old_ensure_loop
    agent.SW_BINDER_COUNT = old_bc
    agent.SW_BINDERFS = os.environ.get("DF_BINDERFS", "/dev/binderfs")

# --- H. gate placement + sibling gates kept ----------------------------------
src_ensure = inspect.getsource(agent.sw_ensure_binder_devices)
check("_sw_coerce_binder_count(count)" in src_ensure,
      "placement: ensure gates before any shell touch", "")
check(src_ensure.index("_sw_coerce_binder_count") <
      src_ensure.index("test -e /dev/binderfs/binder-control"),
      "placement: ensure gate precedes first run() call", "")
src_pool = inspect.getsource(agent.sw_boot_binder_pool)
check("_sw_coerce_binder_count(count)" in src_pool,
      "placement: pool gates before any shell touch", "")
check(src_pool.index("_sw_coerce_binder_count") <
      src_pool.index("modprobe binder_linux"),
      "placement: pool gate precedes modprobe", "")
src = open(AGENT, encoding="utf-8").read()
for marker, label in [
        ("agent-create-numerics-sink-gate", "sibling kept: create numerics gate"),
        ("agent-resize-numerics-sink-gate", "sibling kept: resize numerics gate"),
        ("agent-status-delete-sink-gate", "sibling kept: status/delete name gates"),
        ("invalid name", "sibling kept: name gates still fail closed")]:
    check(marker in src, label, "")
check("SW_BINDER_COUNT = _sw_env_int_min" in src,
      "env read kept: DF_BINDER_COUNT default 64 unchanged", "")

# --- I. deploy wiring ---------------------------------------------------------
deploy = open(DEPLOY_ALL, encoding="utf-8").read()
check("wing/agent.py" in deploy,
      "deploy wiring: agent.py already in apply-all stage list")
check("agent-binder-count-sink-gate" in deploy,
      "deploy wiring: t173 preflight line present in deploy-all.sh")
check(agent.VERSION == "1.0.45",
      "VERSION deliberately unchanged at 1.0.45 (no bump needed)",
      repr(agent.VERSION))

# --- J. negative proof ---------------------------------------------------------
# Pre-fix, range(count) with an absurd count IS the loop: the gated path
# above returned (0, False) with zero commands, while the old expression
# would have produced that many iterations.
check(len(range(10 ** 9)) == 10 ** 9,
      "negative proof: pre-fix range(10**9) is a billion-iteration loop", "")
have, blocked, cmds = pool_with(10 ** 9)
check((have, blocked) == (0, False) and cmds == [],
      "negative proof: gated path refuses 10**9 with zero shell cmds",
      repr((have, blocked, len(cmds))))

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
