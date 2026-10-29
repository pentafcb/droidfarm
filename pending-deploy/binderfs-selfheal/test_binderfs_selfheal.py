#!/usr/bin/env python3
"""Tests for manager ensure_binder_devices() (binderfs self-heal on create).

Commit 4928452 added ensure_binder_devices(): when instance creation finds no
free binder triplet, the manager recreates the binderfs device pool (which a
host reboot drops back to the stock set) and retries once before failing.

The function runs `mount` and writes to /dev/binderfs/binder-control as root
on the manager host, and it gates instance creation for the whole farm — a
bug here wedges "Add instance" for every node. These tests run it against a
fake run() that simulates /dev/binderfs state transitions, hermetically.

Covers:
  A. ensure_binder_devices() — full pool already present -> False, no writes
  B. stock set only -> creates binder..binder63 missing names, returns True
  C. binder-control missing + mount succeeds -> proceeds, returns True
  D. binder-control missing + mount fails -> False, no creation attempted
  E. device creation failure tolerated -> False, no raise
  F. count parameter respected (count=3)
  G. end-to-end: no triplet -> ensure -> retry -> triplet from new devices
  H. safety: generated names contain no shell metachars
  I. module missing + modprobe fails -> modprobe attempted, False, no writes
  J. module missing + modprobe succeeds -> mount proceeds, 64 devices, True
  K. modprobe never called when binder-control already present
  L. REGRESSION (2026-10-13): modprobe never takes a devices= value (the
     binder 'devices' module parameter is a comma-separated NAME list, not
     a count — passing devices=64 creates ONE device literally named "64"
     and zero /dev/binder* nodes, observed on vultr)
  M. install.sh: persistent modprobe.d config uses the name list, never a
     numeric devices= value; the marked generator yields exactly the 64
     expected names (extracted verbatim from install.sh and executed)
"""
import importlib.util
import inspect
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")

PASS = 0
FAIL = 0


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


def load_app():
    spec = importlib.util.spec_from_file_location("df_app", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeBinderFS:
    """Simulates /dev/binderfs for the module-level run()."""

    def __init__(self, devices=(), control=True, mount_ok=True, create_ok=True,
                 module_loaded=True, modprobe_ok=True):
        self.devices = set(devices)
        self.control = control      # /dev/binderfs/binder-control exists
        self.mount_ok = mount_ok    # mount attempt succeeds (needs the module)
        self.create_ok = create_ok  # echo > binder-control succeeds
        self.module_loaded = module_loaded  # binder_linux loaded
        self.modprobe_ok = modprobe_ok      # modprobe attempt succeeds
        self.cmds = []              # every command seen

    def run(self, cmd, timeout=30):
        self.cmds.append(cmd)
        c = cmd.replace(" 2>/dev/null", "")
        if c.startswith("test -e /dev/binderfs/binder-control"):
            return (0, "", "") if self.control else (1, "", "")
        if c.startswith("modprobe binder_linux"):
            if self.modprobe_ok:
                self.module_loaded = True
                return (0, "", "")
            return (1, "", "modprobe failed")
        if c.startswith("mkdir -p /dev/binderfs"):
            if self.mount_ok and self.module_loaded:
                self.control = True
                return (0, "", "")
            return (1, "", "mount failed")
        m = re.fullmatch(r"test -e /dev/binderfs/(\S+)", c)
        if m:
            return (0, "", "") if m.group(1) in self.devices else (1, "", "")
        m = re.fullmatch(r"echo (\S+) > /dev/binderfs/binder-control", c)
        if m:
            if self.control and self.create_ok:
                self.devices.add(m.group(1))
                return (0, "", "")
            return (1, "", "create failed")
        if c.startswith("ls /dev/binderfs/"):
            if not self.control:
                return (1, "", "")
            return (0, "\n".join(sorted(self.devices)) + "\n", "")
        return (1, "", "unexpected: " + cmd)


def stock26():
    return ["binder"] + [f"binder{i}" for i in range(1, 26)]


def full64():
    return ["binder"] + [f"binder{i}" for i in range(1, 64)]


mod = load_app()

print("== A. full pool already present -> no writes, False ==")
fs = FakeBinderFS(devices=full64())
mod.run = fs.run
r = mod.ensure_binder_devices()
check(r is False, "returns False when pool complete", repr(r))
writes = [c for c in fs.cmds if c.startswith("echo ")]
check(not writes, "no device creation writes", str(writes))

print("== B. stock set only -> creates missing names, True ==")
fs = FakeBinderFS(devices=stock26())
mod.run = fs.run
r = mod.ensure_binder_devices()
check(r is True, "returns True when devices created", repr(r))
missing = [f"binder{i}" for i in range(26, 64)]
check(all(m in fs.devices for m in missing), "binder26..binder63 created",
      str(sorted(fs.devices - set(stock26())))[:80])
check(len(fs.devices) == 64, "pool now 64 devices", str(len(fs.devices)))

print("== C. binder-control missing, mount succeeds ==")
fs = FakeBinderFS(devices=[], control=False, mount_ok=True)
mod.run = fs.run
r = mod.ensure_binder_devices()
check(r is True, "proceeds after successful mount", repr(r))
check(any(c.startswith("mkdir -p") for c in fs.cmds), "mount attempted")
check(len(fs.devices) == 64, "64 devices created after mount", str(len(fs.devices)))

print("== D. binder-control missing, mount fails ==")
fs = FakeBinderFS(devices=[], control=False, mount_ok=False)
mod.run = fs.run
r = mod.ensure_binder_devices()
check(r is False, "returns False when mount fails", repr(r))
writes = [c for c in fs.cmds if c.startswith("echo ")]
check(not writes, "no creation attempted after failed mount", str(writes))

print("== E. creation failure tolerated ==")
fs = FakeBinderFS(devices=[], control=True, create_ok=False)
mod.run = fs.run
try:
    r = mod.ensure_binder_devices()
    check(r is False, "returns False, no raise", repr(r))
except Exception as e:  # noqa: BLE001
    bad("raised", repr(e))

print("== F. count parameter respected ==")
fs = FakeBinderFS(devices=[], control=True)
mod.run = fs.run
r = mod.ensure_binder_devices(count=3)
check(r is True, "returns True", repr(r))
check(fs.devices == {"binder", "binder1", "binder2"}, "exactly binder,binder1,binder2",
      str(sorted(fs.devices)))

print("== G. end-to-end: no triplet -> ensure -> retry -> triplet ==")
fs = FakeBinderFS(devices=stock26(), control=True)
mod.run = fs.run
# Mark all but 2 devices used: no free triplet available.
used = set(stock26()) - {"binder", "binder1"}
t = mod.free_binder_triplet(used)
check(t is None, "no free triplet on stock pool", repr(t))
# The exact retry logic from the create handler (app.py ~1787-1792):
binders = mod.free_binder_triplet(used)
if not binders:
    if mod.ensure_binder_devices():
        binders = mod.free_binder_triplet(used)
check(binders is not None and len(binders) == 3,
      "triplet recovered after self-heal: " + ",".join(binders or []),
      "still no triplet: " + repr(binders))
check(binders is not None and all(b not in used for b in binders),
      "triplet devices are free")

print("== H. no shell metachars in generated names ==")
fs = FakeBinderFS(devices=[], control=True)
mod.run = fs.run
mod.ensure_binder_devices(count=64)
names = re.findall(r"echo (\S+) >", " ".join(fs.cmds))
check(len(names) == 64, "64 names generated", str(len(names)))
check(all(re.fullmatch(r"binder(\d+)?", n) for n in names),
      "names strictly binder/binderN", str(names[:5]))
META = re.compile(r"[;|&$`'\"\\(){}\n]")
check(not any(META.search(n) for n in names), "no metachars in any name")

print("== I. module missing + modprobe fails -> attempted, False, no writes ==")
fs = FakeBinderFS(devices=[], control=False, mount_ok=True,
                  module_loaded=False, modprobe_ok=False)
mod.run = fs.run
r = mod.ensure_binder_devices()
check(any(c.startswith("modprobe binder_linux") for c in fs.cmds),
      "modprobe attempted when binder-control missing")
check(r is False, "returns False when module unavailable and mount fails", repr(r))
writes = [c for c in fs.cmds if c.startswith("echo ")]
check(not writes, "no creation attempted", str(writes))

print("== J. module missing + modprobe succeeds -> mount proceeds, 64 devices ==")
fs = FakeBinderFS(devices=[], control=False, mount_ok=True,
                  module_loaded=False, modprobe_ok=True)
mod.run = fs.run
r = mod.ensure_binder_devices()
check(any(c.startswith("modprobe binder_linux") for c in fs.cmds),
      "modprobe attempted when binder-control missing")
check(r is True, "returns True after modprobe+mount", repr(r))
check(len(fs.devices) == 64, "64 devices created", str(len(fs.devices)))

print("== K. modprobe never called when binder-control already present ==")
fs = FakeBinderFS(devices=full64())
mod.run = fs.run
mod.ensure_binder_devices()
check(not any(c.startswith("modprobe") for c in fs.cmds),
      "no modprobe on the healthy path")

print("== L. REGRESSION: modprobe never takes a devices= value ==")
# The binder 'devices' module parameter is a comma-separated NAME list
# (kernel CONFIG_ANDROID_BINDER_DEVICES semantics), NOT a count. Passing
# devices=64 creates a single device literally named "64" and zero
# /dev/binder* nodes — this exact mistake shipped once (2026-10-13) and
# produced no binder devices on a fresh host.
fs = FakeBinderFS(devices=[], control=False, mount_ok=True,
                  module_loaded=False, modprobe_ok=True)
mod.run = fs.run
mod.ensure_binder_devices()
mp = [c for c in fs.cmds if "modprobe" in c]
check(len(mp) >= 1, "modprobe was attempted on the missing-module path",
      str(mp))
check(all("devices=" not in c for c in mp),
      "no devices= argument on any modprobe call", str(mp))
src = inspect.getsource(mod.ensure_binder_devices)
mplines = [l for l in src.splitlines()
           if "modprobe" in l and not l.strip().startswith("#")]
check(all("devices=" not in l for l in mplines),
      "no devices= on any modprobe code line in ensure_binder_devices",
      [l.strip() for l in mplines if "devices=" in l][:2])

print("== M. install.sh: persistent binder config uses the name list ==")
ISH = os.path.join(REPO, "install.sh")
ish = open(ISH).read()
code = "\n".join(l for l in ish.splitlines()
                 if not l.strip().startswith("#"))  # comments may name the bug
check(not re.search(r"devices=\d+", code),
      "no numeric devices=<n> value in install.sh code",
      re.findall(r"devices=\S+", code)[:3])
check("modprobe binder_linux devices=" not in ish,
      "no devices= on the runtime modprobe command line")
m = re.search(r"# --- binder-names-begin ---\n(.*?)# --- binder-names-end ---",
              ish, re.S)
check(m is not None, "binder-names generator markers present in install.sh")
names = None
if m is not None:
    try:
        out = subprocess.run(["bash", "-c",
                              m.group(1) + '\necho "$BINDER_NAMES"'],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            names = out.stdout.strip().split(",")
    except Exception as e:  # noqa: BLE001
        bad("generator execution raised", repr(e))
check(names is not None and len(names) == 64,
      "generator yields exactly 64 names",
      repr(len(names)) if names is not None else "no names")
if names is not None:
    check(names[0] == "binder" and names[-1] == "binder63",
          "first=binder last=binder63",
          repr((names[0], names[-1])) if names else "empty")
    check(all(re.fullmatch(r"binder\d*", n) for n in names),
          "every name is binder/binderN", str(names[:4]))
    check(len(set(names)) == 64, "all 64 names unique")

print(f"== binderfs self-heal: PASS={PASS} FAIL={FAIL} ==")
sys.exit(0 if FAIL == 0 else 1)
