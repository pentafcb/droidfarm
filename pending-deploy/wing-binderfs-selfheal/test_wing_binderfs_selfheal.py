#!/usr/bin/env python3
"""Tests for wing agent 1.0.16 binderfs self-heal (manager ensure_binder_devices parity).

REAL GAP: the manager self-heals the binderfs pool on instance create
(2026-10-07 binderfs-selfheal: ensure_binder_devices() recreates the pool
via binder-control when no free triplet exists, then retries once). The
wing agent's create path had no such recovery: a wing node rebooted back to
the stock binder set failed every instance create with "binder devices not
found" until a human recreated the pool by hand. Agent 1.0.16 adds
sw_ensure_binder_devices() and wires it into create_instance_linux
(scan -> self-heal -> rescan once -> fail honestly).

This suite covers:

  A. agent.VERSION is exactly 1.0.16 + manager >=1.0.2 gate compatibility
  B. sw_find_binder_triplet: binder scheme / binder0 scheme / incomplete /
     missing base / never raises
  C. sw_ensure_binder_devices: full pool -> False, no creation writes
  D. short pool -> creates exactly the missing names, True
  E. binder-control missing + mount succeeds -> proceeds, True
  F. binder-control missing + mount fails -> False, no creation attempted
  G. creation failure tolerated -> False, no raise
  H. count parameter respected (count=3)
  I. module missing + modprobe fails -> modprobe attempted, False, no writes
  J. module missing + modprobe succeeds -> mount proceeds, True
  K. modprobe never called when binder-control already present
  L. REGRESSION: modprobe never takes a devices=<count> value (the binder
     'devices' module parameter is a comma-separated NAME list, not a
     count — devices=64 creates ONE device literally named "64" and zero
     /dev/binder* nodes)
  M. generated names contain no shell metachars
  N. create path end-to-end: no triplet -> self-heal -> triplet ->
     docker run with the self-healed binds
  O. create path: unhealable pool -> (False, "binder devices not found"),
     no docker run attempted
  P. create path: triplet already present -> no self-heal shell writes at all
  Q. wiring: IS_WINDOWS guard returns False; create_instance_linux sources
     reference both helpers; sw_ensure_binder_devices never raises on garbage
"""
import importlib.util
import inspect
import os
import re
import sys

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


agent = load("wbs_agent", AGENT)
app = load("wbs_app", APP)


class FakeShell:
    """Simulates the agent's 2-tuple run(cmd, timeout=30, shell=True) against
    a fake /dev/binderfs. Mirrors the manager binderfs-selfheal fixture,
    adapted to the agent's (rc, out) return shape."""

    def __init__(self, devices=(), control=True, mount_ok=True, create_ok=True,
                 module_loaded=True, modprobe_ok=True):
        self.devices = set(devices)
        self.control = control
        self.mount_ok = mount_ok
        self.create_ok = create_ok
        self.module_loaded = module_loaded
        self.modprobe_ok = modprobe_ok
        self.cmds = []

    def run(self, cmd, timeout=30, shell=False):
        self.cmds.append(cmd)
        c = cmd.replace(" 2>/dev/null", "")
        if c == "test -e /dev/binderfs/binder-control":
            return (0, "") if self.control else (1, "")
        if c.startswith("modprobe binder_linux"):
            if self.modprobe_ok:
                self.module_loaded = True
                return (0, "")
            return (1, "modprobe failed")
        if c.startswith("mkdir -p /dev/binderfs"):
            if self.mount_ok and self.module_loaded:
                self.control = True
                return (0, "")
            return (1, "mount failed")
        m = re.fullmatch(r"test -e /dev/binderfs/(\S+)", c)
        if m:
            return (0, "") if m.group(1) in self.devices else (1, "")
        m = re.fullmatch(r"echo (\S+) > /dev/binderfs/binder-control", c)
        if m:
            if self.control and self.create_ok:
                self.devices.add(m.group(1))
                return (0, "")
            return (1, "create failed")
        return (1, "unexpected: " + cmd)


_real_exists = os.path.exists


class FakeExists:
    """os.path.exists stand-in: consults the FakeShell's device set for
    /dev/binderfs/*, delegates everything else to the real os.path.exists."""

    def __init__(self, fs):
        self.fs = fs

    def __call__(self, path):
        if isinstance(path, str) and path.startswith("/dev/binderfs/"):
            if path == "/dev/binderfs/binder-control":
                return self.fs.control
            return path.rsplit("/", 1)[1] in self.fs.devices
        return _real_exists(path)


def with_fakes(fs, body):
    """Run body() with agent.run and os.path.exists faked; restore after."""
    agent.run, old_run = fs.run, agent.run
    os.path.exists = FakeExists(fs)
    try:
        body()
    finally:
        agent.run = old_run
        os.path.exists = _real_exists


def stock26():
    return ["binder"] + [f"binder{i}" for i in range(1, 26)]


def full64():
    return ["binder"] + [f"binder{i}" for i in range(1, 64)]


print("== A. VERSION bump + manager gate ==")
check(agent.VERSION in ("1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"),
      "A1 VERSION is 1.0.16+ (exact bump now owned by wing-agent-selfcheck)",
      repr(agent.VERSION))
check(app._version_at_least("1.0.16", "1.0.2") and not app._version_at_least("1.0.1", "1.0.2"),
      "A2 manager >=1.0.2 gate accepts 1.0.16", "gate contract broken")

print("== B. sw_find_binder_triplet ==")


def triplet(fs_devices):
    fs = FakeShell(devices=fs_devices)
    out = []

    def body():
        out.append(agent.sw_find_binder_triplet())
    with_fakes(fs, body)
    return out[0]


t = triplet(full64())
check(t == ["/dev/binderfs/binder", "/dev/binderfs/binder1", "/dev/binderfs/binder2"],
      "B1 binder scheme yields [binder, binder1, binder2]", repr(t))
t = triplet(["binder0", "binder1", "binder2"])
check(t == ["/dev/binderfs/binder0", "/dev/binderfs/binder1", "/dev/binderfs/binder2"],
      "B2 binder0 scheme yields [binder0, binder1, binder2]", repr(t))
check(triplet(["binder", "binder1"]) is None, "B3 incomplete triplet -> None")
check(triplet([]) is None, "B4 empty pool -> None")
try:
    out = agent.sw_find_binder_triplet()
    check(True, "B5 never raises on the real (binder-less) host", repr(out))
except Exception as e:  # noqa: BLE001
    bad("B5 never raises", repr(e))

print("== C. full pool -> no writes, False ==")
fs = FakeShell(devices=full64())
agent.run, old_run = fs.run, agent.run
try:
    r = agent.sw_ensure_binder_devices()
finally:
    agent.run = old_run
check(r is False, "C1 returns False when pool complete", repr(r))
check(not [c for c in fs.cmds if c.startswith("echo ")],
      "C2 no device creation writes", str(fs.cmds))

print("== D. short pool -> creates missing, True ==")
fs = FakeShell(devices=stock26())
agent.run, old_run = fs.run, agent.run
try:
    r = agent.sw_ensure_binder_devices()
finally:
    agent.run = old_run
check(r is True, "D1 returns True when devices created", repr(r))
missing = [f"binder{i}" for i in range(26, 64)]
check(all(m in fs.devices for m in missing), "D2 binder26..binder63 created",
      str(sorted(fs.devices - set(stock26())))[:60])
check(len(fs.devices) == 64, "D3 pool now 64 devices", str(len(fs.devices)))

print("== E. binder-control missing, mount succeeds ==")
fs = FakeShell(devices=[], control=False, mount_ok=True)
agent.run, old_run = fs.run, agent.run
try:
    r = agent.sw_ensure_binder_devices()
finally:
    agent.run = old_run
check(r is True, "E1 proceeds after successful mount", repr(r))
check(any(c.startswith("mkdir -p") for c in fs.cmds), "E2 mount attempted")
check(len(fs.devices) == 64, "E3 64 devices created after mount", str(len(fs.devices)))

print("== F. binder-control missing, mount fails ==")
fs = FakeShell(devices=[], control=False, mount_ok=False)
agent.run, old_run = fs.run, agent.run
try:
    r = agent.sw_ensure_binder_devices()
finally:
    agent.run = old_run
check(r is False, "F1 returns False when mount fails", repr(r))
check(not [c for c in fs.cmds if c.startswith("echo ")],
      "F2 no creation attempted after failed mount", str(fs.cmds))

print("== G. creation failure tolerated ==")
fs = FakeShell(devices=[], control=True, create_ok=False)
agent.run, old_run = fs.run, agent.run
try:
    r = agent.sw_ensure_binder_devices()
    check(r is False, "G1 returns False, no raise", repr(r))
except Exception as e:  # noqa: BLE001
    bad("G1 raised", repr(e))
finally:
    agent.run = old_run

print("== H. count parameter respected ==")
fs = FakeShell(devices=[], control=True)
agent.run, old_run = fs.run, agent.run
try:
    r = agent.sw_ensure_binder_devices(count=3)
finally:
    agent.run = old_run
check(r is True, "H1 returns True", repr(r))
check(fs.devices == {"binder", "binder1", "binder2"},
      "H2 exactly binder,binder1,binder2 created", str(sorted(fs.devices)))

print("== I. module missing + modprobe fails ==")
fs = FakeShell(devices=[], control=False, mount_ok=True,
               module_loaded=False, modprobe_ok=False)
agent.run, old_run = fs.run, agent.run
try:
    r = agent.sw_ensure_binder_devices()
finally:
    agent.run = old_run
check(any(c.startswith("modprobe binder_linux") for c in fs.cmds),
      "I1 modprobe attempted", str(fs.cmds))
check(r is False, "I2 returns False", repr(r))
check(not [c for c in fs.cmds if c.startswith("echo ")],
      "I3 no creation writes when module is missing", str(fs.cmds))

print("== J. module missing + modprobe succeeds ==")
fs = FakeShell(devices=[], control=False, mount_ok=True,
               module_loaded=False, modprobe_ok=True)
agent.run, old_run = fs.run, agent.run
try:
    r = agent.sw_ensure_binder_devices()
finally:
    agent.run = old_run
check(r is True, "J1 proceeds after modprobe+mount", repr(r))
check(len(fs.devices) == 64, "J2 64 devices created", str(len(fs.devices)))

print("== K. modprobe never called when binder-control present ==")
fs = FakeShell(devices=stock26(), control=True)
agent.run, old_run = fs.run, agent.run
try:
    agent.sw_ensure_binder_devices()
finally:
    agent.run = old_run
check(not any(c.startswith("modprobe") for c in fs.cmds),
      "K1 no modprobe when binder-control exists", str(fs.cmds))

print("== L. modprobe never takes devices=<count> ==")
fs = FakeShell(devices=[], control=False, mount_ok=True,
               module_loaded=False, modprobe_ok=True)
agent.run, old_run = fs.run, agent.run
try:
    agent.sw_ensure_binder_devices()
finally:
    agent.run = old_run
mods = [c for c in fs.cmds if c.startswith("modprobe")]
check(mods and all("devices=" not in c for c in mods),
      "L1 no devices= on the modprobe line", str(mods))

print("== M. name safety ==")
fs = FakeShell(devices=[], control=True)
agent.run, old_run = fs.run, agent.run
try:
    agent.sw_ensure_binder_devices()
finally:
    agent.run = old_run
writes = [c for c in fs.cmds if c.startswith("echo ")]
names = [re.fullmatch(r"echo (\S+) > /dev/binderfs/binder-control 2>/dev/null", c).group(1)
         for c in writes]
check(all(re.fullmatch(r"binder\d*", n) for n in names),
      "M1 every created name matches binder\\d* (no shell metachars)",
      str(names[:5]))


class FakeDocker:
    def __init__(self, net):
        self.net = net
        self.calls = []

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[0] == "ps":
            return 0, ""
        if args[0] == "network" and args[1] == "ls":
            return 0, self.net
        if args[0] == "run":
            return 0, "deadbeef\n"
        return 0, ""


def run_create(fs, name="b1"):
    """Drive create_instance_linux with faked docker/instances/run/exists.
    Returns (ok, msg, fake_docker, fs)."""
    fd = FakeDocker(agent.NETWORK)
    saved = {}
    out = []

    def body():
        out.append(agent.create_instance_linux(name, 2, 2048, "10.20.0.21"))

    saved["docker"] = agent.docker
    saved["load"] = agent.load_instances
    saved["save"] = agent.save_instances
    saved["loop"] = agent.sw_ensure_loop_devices
    agent.docker = fd
    agent.load_instances = lambda: {}
    agent.save_instances = lambda d: saved.update(state=d)
    agent.sw_ensure_loop_devices = lambda: True
    with_fakes(fs, body)
    agent.docker = saved["docker"]
    agent.load_instances = saved["load"]
    agent.save_instances = saved["save"]
    agent.sw_ensure_loop_devices = saved["loop"]
    return out[0], fd, fs


print("== N. create: no triplet -> self-heal -> docker run ==")
fs = FakeShell(devices=[], control=True)  # pool empty, binder-control present
(ok1, msg), fd, fs = run_create(fs)
check(ok1 is True, "N1 create succeeds after self-heal", repr((ok1, msg)))
check(any(c.startswith("echo ") for c in fs.cmds), "N2 self-heal wrote devices")
runs = [c for c in fd.calls if c[0] == "run"]
check(len(runs) == 1, "N3 exactly one docker run", str(len(runs)))
vargs = runs[0] if runs else ()
check("/dev/binderfs/binder:/dev/binder" in vargs and
      "/dev/binderfs/binder1:/dev/vndbinder" in vargs and
      "/dev/binderfs/binder2:/dev/hwbinder" in vargs,
      "N4 binds use the self-healed triplet", str(vargs[:8]))
check(len(fs.devices) == 64, "N5 pool now 64 devices", str(len(fs.devices)))

print("== O. create: unhealable pool -> honest failure, no docker run ==")
fs = FakeShell(devices=[], control=False, mount_ok=False,
               module_loaded=False, modprobe_ok=False)
(ok1, msg), fd, fs = run_create(fs)
check(ok1 is False and msg == "binder devices not found",
      "O1 fails with 'binder devices not found'", repr((ok1, msg)))
check(not [c for c in fd.calls if c[0] == "run"],
      "O2 no docker run attempted on unhealable pool", str(fd.calls[:3]))

print("== P. create: triplet present -> no self-heal writes ==")
fs = FakeShell(devices=["binder", "binder1", "binder2"], control=True)
(ok1, msg), fd, fs = run_create(fs)
check(ok1 is True, "P1 create succeeds", repr((ok1, msg)))
check(not fs.cmds, "P2 zero shell writes when the triplet exists", str(fs.cmds))

print("== Q. wiring + never-raises ==")
src = inspect.getsource(agent.create_instance_linux)
check("sw_find_binder_triplet" in src and "sw_ensure_binder_devices" in src,
      "Q1 create path references both helpers")
try:
    r = agent.sw_ensure_binder_devices(count="garbage")
    check(r is False, "Q2 garbage count -> False, no raise", repr(r))
except Exception as e:  # noqa: BLE001
    bad("Q2 garbage count raised", repr(e))
try:
    r = agent.sw_ensure_binder_devices(count=0)
    check(r is False, "Q3 count=0 -> False, no raise", repr(r))
except Exception as e:  # noqa: BLE001
    bad("Q3 count=0 raised", repr(e))
check("IS_WINDOWS" in inspect.getsource(agent.sw_ensure_binder_devices),
      "Q4 Windows guard present in sw_ensure_binder_devices")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
