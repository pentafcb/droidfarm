"""Unit tests for the loop-device guard.

Gap: the Waydroid fleet needs ~2 loop devices per instance at boot (the
2026-10-08 recovery needed ~40 for 20), but only binder devices were
provisioned persistently. install.sh created /dev/loop0..63 with a one-shot
mknod that does NOT survive reboot on devtmpfs, the manager's create-time
self-heal (ensure_binder_devices) never touched loop, and verify-binder.sh
only checked the binder chain — so a post-reboot host silently capped the
fleet at ~4 booting instances with no actionable error.

Now: manager/app.py gains count_loop_devices() + ensure_loop_devices()
(modprobe loop max_loop=64 when the module isn't loaded, mknod for missing
nodes, honest False when the live driver's max_loop is too small);
create_local_instance() self-heals then fails OPEN with a loud "warning" on
the 200 response (loop is consumed at boot, not reserved at create — a small
fleet runs fine on the stock 8); fleet_headroom() (local branch) reports
loop_devices + a plan warning; install.sh step 4 persists the pool via
/etc/modprobe.d/loop.conf + /etc/modules; verify-binder.sh checks the whole
loop chain (nodes, max_loop capacity, persistence via GRUB cmdline or
modprobe.d).

Run: python3 test_loop_device_guard.py

Hermetic: load_instances/save_instances/run/next_free_ip/get_host_stats are
monkeypatched; no docker, no network, no /dev writes.
"""
import importlib.util
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

app = load("loopguard_app", os.path.join(HERE, "..", "..", "manager", "app.py"))

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")

# ---------------- fakes ----------------
RUN_CMDS = []
INSTANCES = {}
PROVIDERS = {}
LOOP_NODES = set()      # ints present as /dev/loopN
LOOP_MODULE = True      # /sys/module/loop visible
LOOP_MAXLOOP = 64       # /sys/module/loop/parameters/max_loop value
LOOP_LS_MODE = "nodes"  # nodes | empty | garbage | raise
BINDER_LS = " ".join(["binder"] + [f"binder{i}" for i in range(1, 64)])

def fake_load_instances():
    return {k: dict(v) for k, v in INSTANCES.items()}
def fake_save_instances(d):
    INSTANCES.clear()
    INSTANCES.update({k: dict(v) for k, v in d.items()})
def fake_load_providers():
    return {k: dict(v) for k, v in PROVIDERS.items()}

def fake_run(cmd, timeout=30):
    RUN_CMDS.append(cmd)
    if LOOP_LS_MODE == "raise":
        raise RuntimeError("boom")
    if cmd.startswith("ls -d /dev/loop"):
        if LOOP_LS_MODE == "empty":
            return 1, "", ""
        if LOOP_LS_MODE == "garbage":
            return 0, "/dev/loopX\n/dev/loop\nloop5\n", ""
        return 0, "\n".join(f"/dev/loop{i}" for i in sorted(LOOP_NODES)), ""
    if cmd.startswith("test -d /sys/module/loop"):
        return (0, "", "") if LOOP_MODULE else (1, "", "")
    if cmd.startswith("modprobe loop"):
        # a real modprobe on an unloaded module creates the devtmpfs nodes
        if not LOOP_MODULE:
            globals()["LOOP_MODULE"] = True
            m = re.search(r"max_loop=(\d+)", cmd)
            globals()["LOOP_MAXLOOP"] = int(m.group(1)) if m else 8
            LOOP_NODES.update(range(globals()["LOOP_MAXLOOP"]))
        return 0, "", ""
    if cmd.startswith("mknod /dev/loop"):
        m = re.search(r"/dev/loop(\d+)", cmd)
        if m:
            LOOP_NODES.add(int(m.group(1)))
        return 0, "", ""
    if cmd.startswith("cat /sys/module/loop/parameters/max_loop"):
        if LOOP_MODULE:
            return 0, str(LOOP_MAXLOOP), ""
        return 1, "", ""
    if cmd.startswith("ls /dev/binderfs/"):
        return 0, BINDER_LS, ""
    if cmd.startswith("docker image inspect"):
        return 0, '[{"Id":"sha256:abc"}]', ""
    if cmd.startswith("docker inspect"):
        return 1, "", ""
    if cmd.startswith("docker run"):
        return 0, "cid123", ""
    return 0, "", ""

def fake_next_free_ip(instances):
    used = {v.get("ip") for v in instances.values() if v.get("ip")}
    for i in range(11, 250):
        ip = f"10.20.0.{i}"
        if ip not in used:
            return ip
    return None
def fake_get_host_stats():
    return {"ram_total_mb": 31744, "ram_percent": 50.0}

def reset(loop_nodes=64, module=True, maxloop=64, ls_mode="nodes"):
    global RUN_CMDS, LOOP_NODES, LOOP_MODULE, LOOP_MAXLOOP, LOOP_LS_MODE
    RUN_CMDS = []
    LOOP_NODES = set(range(loop_nodes))
    LOOP_MODULE = module
    LOOP_MAXLOOP = maxloop
    LOOP_LS_MODE = ls_mode
    INSTANCES.clear()
    PROVIDERS.clear()
    app.load_instances = fake_load_instances
    app.save_instances = fake_save_instances
    app.load_providers = fake_load_providers
    app.run = fake_run
    app.next_free_ip = fake_next_free_ip
    app.get_host_stats = fake_get_host_stats

# ================= A. count_loop_devices() =================
reset(64)
check("A1 64 nodes -> 64", app.count_loop_devices() == 64)

reset(8)
check("A2 8 nodes -> 8", app.count_loop_devices() == 8)

reset(64, ls_mode="empty")
check("A3 ls rc!=0 -> 0", app.count_loop_devices() == 0)

reset(64, ls_mode="garbage")
check("A4 garbage tokens ignored -> 0", app.count_loop_devices() == 0)

reset(64, ls_mode="raise")
check("A5 run raising -> 0, never raises", app.count_loop_devices() == 0)

# ================= B. ensure_loop_devices() =================
reset(64)
check("B1 healthy pool -> True, no modprobe/mknod",
      app.ensure_loop_devices() is True
      and not any(c.startswith("modprobe loop") for c in RUN_CMDS)
      and not any(c.startswith("mknod") for c in RUN_CMDS))

reset(8, module=True, maxloop=64)
ok = app.ensure_loop_devices()
check("B2 stock-8 pool, capable driver -> mknod 8..63, True",
      ok is True and len(LOOP_NODES) == 64
      and sum(1 for c in RUN_CMDS if c.startswith("mknod /dev/loop")) == 56)

reset(0, module=False)
ok = app.ensure_loop_devices()
check("B3 module not loaded -> modprobe loop max_loop=64, True",
      ok is True
      and any(c.startswith("modprobe loop max_loop=64") for c in RUN_CMDS))

reset(8, module=True, maxloop=8)
ok = app.ensure_loop_devices()
check("B4 live max_loop=8 -> False (mknod nodes would be unusable)",
      ok is False)

reset(64, ls_mode="raise")
check("B5 run raising -> False, never raises",
      app.ensure_loop_devices() is False)

reset(64)
check("B6 garbage count/floor -> False, never raises",
      app.ensure_loop_devices(count="x") is False)

reset(8, module=True, maxloop=64)
app.ensure_loop_devices()
_loop_cmds = [c for c in RUN_CMDS if "loop" in c]
check("B7 no shell metachars in any loop command",
      len(_loop_cmds) > 0 and all(
          not any(ch in c for ch in ";|&$`\"'()") for c in _loop_cmds))

# ================= C. create_local_instance() wiring =================
reset(8, module=True, maxloop=8)   # unfixable host: warn, but proceed
code, resp = app.create_local_instance("srt1", 2, 2048)
check("C1 loop-broken host -> 200 (fail-open), warning on response",
      code == 200 and resp.get("ok") is True
      and "loop" in resp.get("warning", "").lower())
check("C2 reservation kept on fail-open path", "srt1" in INSTANCES)

reset(64)  # healthy host: silent
code, resp = app.create_local_instance("srt2", 2, 2048)
check("C3 healthy host -> 200, no warning key",
      code == 200 and "warning" not in resp)

reset(64)
app.run = lambda cmd, timeout=30: (1, "", "") if cmd.startswith("ls /dev/binderfs/") else fake_run(cmd, timeout)
# ensure_binder_devices will also fail (no binderfs) -> classic 500
code, resp = app.create_local_instance("srt3", 2, 2048)
check("C4 binder-missing regression: still 500 no free binder devices",
      code == 500 and resp.get("error") == "no free binder devices")
check("C5 no reservation leaked on binder refusal", "srt3" not in INSTANCES)
app.run = fake_run

# gate ordering: the loop self-heal runs before `docker run`
src = open(os.path.join(HERE, "..", "..", "manager", "app.py")).read()
m = re.search(r"def create_local_instance\(.*?\n(?=def |\Z)", src, re.S)
body = m.group(0)
# gate ordering: the loop self-heal runs before the real `docker run -d`
# command (the word "docker run" also appears in an earlier comment)
check("C6 loop self-heal precedes docker run",
      body.find("ensure_loop_devices()") != -1
      and body.find('"docker run -d') != -1
      and body.find("ensure_loop_devices()") < body.find('"docker run -d'))

# ================= D. fleet_headroom() =================
reset(8)
hr = app.fleet_headroom({}, "local", 5, 2048)
check("D1 local: loop_devices counted", hr["loop_devices"] == 8)
check("D2 local low loop -> warning names loop + floor",
      "loop" in hr["warning"].lower() and "60" in hr["warning"])

reset(64)
hr = app.fleet_headroom({}, "local", 5, 2048)
check("D3 local healthy loop -> no loop warning",
      hr["loop_devices"] == 64 and "loop device" not in hr["warning"].lower())

reset(8)
PROVIDERS["p1"] = {"stats": {"ram_total_mb": 8000}}
hr = app.fleet_headroom({}, "p1", 5, 2048)
check("D4 remote branch: loop_devices None, no loop warning",
      hr["loop_devices"] is None and "loop device" not in hr["warning"].lower())

reset(8, ls_mode="raise")
hr = app.fleet_headroom({}, "local", 5, 2048)
check("D5 run raising -> never raises, loop_devices None",
      hr["loop_devices"] is None)

# ================= E. constants =================
check("E1 FLEET_LOOP_DEVICE_COUNT == 64", app.FLEET_LOOP_DEVICE_COUNT == 64)
check("E2 FLEET_LOOP_DEVICE_FLOOR == 60", app.FLEET_LOOP_DEVICE_FLOOR == 60)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
