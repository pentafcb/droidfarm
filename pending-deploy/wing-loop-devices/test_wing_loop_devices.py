#!/usr/bin/env python3
"""Tests for wing agent 1.0.12 loop-device guard + installer provisioning.

Fresh wing nodes had no loop-device story: the main node got persistent
provisioning (modprobe.d name list), create-time self-heal, and scripted
verification in loop-device-guard (t66), but wing/install-wing.sh only
provisioned binder and the agent's selfwatch never looked at /dev/loop*.
A rebooted wing node silently exposed the stock 8 loop devices, capping the
node at ~4 booting Waydroid instances with no actionable error.

This suite covers:

  A. agent.VERSION 1.0.12 + manager >=1.0.2 gate compatibility
  B. sw_count_loop_devices units (names, loop-control/partition exclusion)
  C. healthy pool -> no-op (no modprobe/mknod, no marker)
  D. short pool -> modprobe (only when unloaded) + mknod only-missing,
     recoveries bump, success log
  E. unfixable pool -> loop.degraded marker (reason first line), single
     ALERT, no provisioning retry while degraded (alert-only)
  F. live-driver max_loop honesty check (provisioned nodes the driver
     cannot use are reported honestly, not trusted)
  G. relief -> marker removed, relieved log
  H. never raises (raising run, corrupt state dir)
  I. selfwatch_loop wiring contract (host-level, once per pass)
  J. create_instance_linux fail-open (short pool warns, creation proceeds)
  K. loop.degraded flows through selfwatch_state() (dashboard visibility)
  L. install-wing.sh contracts: delimited install_loop_pool block, called
     after binder setup, persistent config + immediate provisioning,
     mknod only-missing, 60-floor warning, no apostrophes inside the
     single-quoted installer block (regression for the exact syntax bug
     caught during development)
  M. deploy-all.sh bash -n gate covers wing/install-wing.sh
"""
import importlib.util
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")
INSTALL_SH = os.path.join(REPO, "wing", "install-wing.sh")
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


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("wld_agent", AGENT)
app = load("wld_app", APP)


def fresh_devdir(*nodes):
    """Real tmp dir materialized with the given loop node files."""
    d = tempfile.mkdtemp(prefix="wld-dev-")
    for n in nodes:
        open(os.path.join(d, n), "w").close()
    return d


def loop_nodes(n):
    return ["loop%d" % i for i in range(n)]


class FakeRun:
    """Stand-in for agent.run: models the loop module + /dev nodes.

    mknod creates real files in the tmp dev dir (sw_count_loop_devices
    lists the real dir). docker calls raise — loop code never calls docker.
    """

    def __init__(self, devdir, module_loaded=False, max_loop_param="64",
                 modprobe_fails=False, mknod_fails=False, raise_all=False):
        self.devdir = devdir
        self.module_loaded = module_loaded
        self.max_loop_param = max_loop_param
        self.modprobe_fails = modprobe_fails
        self.mknod_fails = mknod_fails
        self.raise_all = raise_all
        self.calls = []

    def __call__(self, cmd, timeout=30, shell=False):
        if self.raise_all:
            raise RuntimeError("boom")
        self.calls.append(list(cmd))
        if cmd and cmd[0] == "docker":
            raise AssertionError("loop code called docker: %r" % (cmd,))
        if cmd[:2] == ["test", "-d"]:
            return (0, "") if (cmd[2].endswith("/loop") and self.module_loaded) else (1, "")
        if cmd[:2] == ["modprobe", "loop"]:
            if self.modprobe_fails:
                return 1, "modprobe failed"
            self.module_loaded = True
            return 0, ""
        if cmd[0] == "mknod":
            if self.mknod_fails:
                return 1, "mknod failed"
            open(os.path.join(self.devdir, os.path.basename(cmd[1])), "w").close()
            return 0, ""
        if cmd[0] == "cat" and cmd[1].endswith("max_loop"):
            if self.max_loop_param is None:
                return 1, ""
            return 0, self.max_loop_param
        raise AssertionError("unexpected run call: %r" % (cmd,))

    def modprobe_calls(self):
        return [c for c in self.calls if c[:2] == ["modprobe", "loop"]]

    def mknod_calls(self):
        return [c for c in self.calls if c[0] == "mknod"]


def st_dir():
    return tempfile.mkdtemp(prefix="wld-st-")


def log_text(d):
    p = os.path.join(d, "selfwatch.log")
    try:
        with open(p) as fh:
            return fh.read()
    except Exception:
        return ""


# ---- A. version ------------------------------------------------------------
if agent.VERSION in ("1.0.12", "1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"):
    ok("A1 VERSION is 1.0.12+")
else:
    bad("A1 VERSION is 1.0.12+", "got %r" % agent.VERSION)
# (Widened for 1.0.13; the exact-bump assertion now lives in the
# wing-mem-guard suite, like the 1.0.11->1.0.12 precedent.)

if app._version_at_least("1.0.12", "1.0.2"):
    ok("A2 manager >=1.0.2 gate accepts 1.0.12")
else:
    bad("A2 manager >=1.0.2 gate accepts 1.0.12")

# ---- B. counter units ------------------------------------------------------
d = fresh_devdir(*loop_nodes(8))
if agent.sw_count_loop_devices(d) == 8:
    ok("B1 counts 8 loop nodes")
else:
    bad("B1 counts 8 loop nodes", "got %d" % agent.sw_count_loop_devices(d))
d2 = fresh_devdir("loop0", "loop-control", "loop1", "loop2p1", "loopX", "sda")
if agent.sw_count_loop_devices(d2) == 2:
    ok("B2 ignores loop-control / loopNp1 / non-loop names")
else:
    bad("B2 ignores loop-control / loopNp1 / non-loop names",
        "got %d" % agent.sw_count_loop_devices(d2))
if agent.sw_count_loop_devices("/nonexistent-wld-dir") == 0:
    ok("B3 missing dir -> 0")
else:
    bad("B3 missing dir -> 0")
if agent.sw_count_loop_devices(None) == 0:
    ok("B4 garbage dir -> 0, never raises")
else:
    bad("B4 garbage dir -> 0, never raises")

# ---- C. healthy no-op ------------------------------------------------------
d = fresh_devdir(*loop_nodes(64))
sd = st_dir()
fr = FakeRun(d, module_loaded=True)
agent_run = agent.run
agent.run = fr
try:
    if agent.sw_ensure_loop_devices(st_dir=sd, loop_dir=d) is True:
        ok("C1 64 nodes -> True")
    else:
        bad("C1 64 nodes -> True")
    if not fr.calls:
        ok("C2 healthy pool makes zero run calls (no modprobe/mknod)")
    else:
        bad("C2 healthy pool makes zero run calls", "calls=%r" % (fr.calls,))
    if not os.path.exists(os.path.join(sd, "loop.degraded")):
        ok("C3 healthy pool writes no marker")
    else:
        bad("C3 healthy pool writes no marker")
finally:
    agent.run = agent_run

# ---- D. provisioning -------------------------------------------------------
d = fresh_devdir(*loop_nodes(8))
sd = st_dir()
fr = FakeRun(d, module_loaded=False)
agent.run = fr
try:
    if agent.sw_ensure_loop_devices(st_dir=sd, loop_dir=d) is True:
        ok("D1 8 nodes, unloaded module -> True after provisioning")
    else:
        bad("D1 8 nodes, unloaded module -> True after provisioning")
    mp = fr.modprobe_calls()
    if len(mp) == 1 and mp[0] == ["modprobe", "loop", "max_loop=64"]:
        ok("D2 modprobe loop max_loop=64 issued exactly once")
    else:
        bad("D2 modprobe loop max_loop=64 issued exactly once", "calls=%r" % (mp,))
    mk = fr.mknod_calls()
    mk_names = {os.path.basename(c[1]) for c in mk}
    if mk_names == set(loop_nodes(64)[8:]):
        ok("D3 mknod issued only for the missing loop8..loop63")
    else:
        bad("D3 mknod issued only for the missing loop8..loop63",
            "got %d calls" % len(mk_names))
    if agent.sw_count_loop_devices(d) == 64:
        ok("D4 pool now at 64 nodes")
    else:
        bad("D4 pool now at 64 nodes", "got %d" % agent.sw_count_loop_devices(d))
    import json as _json
    try:
        with open(os.path.join(sd, "recoveries.json")) as fh:
            total = _json.load(fh).get("total", 0)
    except Exception:
        total = 0
    if total == 1 and "provisioned to 64 nodes" in log_text(sd):
        ok("D5 recoveries bumped + success logged")
    else:
        bad("D5 recoveries bumped + success logged", "total=%r" % total)
finally:
    agent.run = agent_run

# module already loaded -> no modprobe, mknod still fills
d = fresh_devdir(*loop_nodes(8))
sd = st_dir()
fr = FakeRun(d, module_loaded=True)
agent.run = fr
try:
    if agent.sw_ensure_loop_devices(st_dir=sd, loop_dir=d) is True:
        ok("D6 loaded module: no modprobe, mknod fills, True")
    else:
        bad("D6 loaded module: no modprobe, mknod fills, True")
    if not fr.modprobe_calls() and len(fr.mknod_calls()) == 56:
        ok("D7 no modprobe call when module already loaded")
    else:
        bad("D7 no modprobe call when module already loaded",
            "modprobe=%d mknod=%d" % (len(fr.modprobe_calls()), len(fr.mknod_calls())))
finally:
    agent.run = agent_run

# ---- E. unfixable pool -> degraded -----------------------------------------
d = fresh_devdir(*loop_nodes(8))
sd = st_dir()
fr = FakeRun(d, module_loaded=True, mknod_fails=True)
agent.run = fr
try:
    if agent.sw_ensure_loop_devices(st_dir=sd, loop_dir=d) is False:
        ok("E1 pool stays short -> False")
    else:
        bad("E1 pool stays short -> False")
    marker = os.path.join(sd, "loop.degraded")
    if os.path.exists(marker):
        with open(marker) as fh:
            reason = fh.readline().strip()
        if reason and len(reason) <= 160:
            ok("E2 loop.degraded marker written, reason on first line")
        else:
            bad("E2 loop.degraded marker written, reason on first line", repr(reason))
    else:
        bad("E2 loop.degraded marker written, reason on first line", "missing")
    if "loop DEGRADED" in log_text(sd):
        ok("E3 single ALERT log line on trip")
    else:
        bad("E3 single ALERT log line on trip")
    before = len(fr.calls)
    if agent.sw_ensure_loop_devices(st_dir=sd, loop_dir=d) is False:
        ok("E4 still False while degraded")
    else:
        bad("E4 still False while degraded")
    if len(fr.calls) == before and log_text(sd).count("WARN loop device pool") == 1:
        ok("E5 degraded pass: no provisioning retry, one WARN (no spam)")
    else:
        bad("E5 degraded pass: no provisioning retry, one WARN (no spam)",
            "new calls=%d warns=%d" % (len(fr.calls) - before,
                                       log_text(sd).count("WARN loop device pool")))
finally:
    agent.run = agent_run

# ---- F. max_loop honesty ----------------------------------------------------
# 64 nodes already present is trusted (manager parity: count >= floor -> True).
# The honesty check fires on the provisioning path: 8 nodes, module loaded
# with max_loop=8 -> mknod fills to 64, but the driver can only use 8.
d = fresh_devdir(*loop_nodes(8))
sd = st_dir()
fr = FakeRun(d, module_loaded=True, max_loop_param="8")
agent.run = fr
try:
    if agent.sw_ensure_loop_devices(st_dir=sd, loop_dir=d) is False:
        ok("F1 provisioned pool but driver max_loop=8 -> False (honest)")
    else:
        bad("F1 provisioned pool but driver max_loop=8 -> False (honest)")
    marker = os.path.join(sd, "loop.degraded")
    if os.path.exists(marker):
        with open(marker) as fh:
            reason = fh.readline().strip()
        if "max_loop" in reason:
            ok("F2 degraded reason names the max_loop capacity problem")
        else:
            bad("F2 degraded reason names the max_loop capacity problem", repr(reason))
    else:
        bad("F2 degraded reason names the max_loop capacity problem", "missing")
    if len(fr.mknod_calls()) == 56:
        ok("F3 honesty check runs after mknod (provisioning attempted first)")
    else:
        bad("F3 honesty check runs after mknod (provisioning attempted first)")
finally:
    agent.run = agent_run

# modprobe fails but mknod works (module unloaded, devtmpfs): still True
d = fresh_devdir(*loop_nodes(8))
sd = st_dir()
fr = FakeRun(d, module_loaded=False, modprobe_fails=True, max_loop_param=None)
agent.run = fr
try:
    if agent.sw_ensure_loop_devices(st_dir=sd, loop_dir=d) is True:
        ok("F3 modprobe fails, mknod fills, no max_loop read -> True")
    else:
        bad("F3 modprobe fails, mknod fills, no max_loop read -> True")
finally:
    agent.run = agent_run

# ---- G. relief ---------------------------------------------------------------
d = fresh_devdir(*loop_nodes(8))
sd = st_dir()
fr = FakeRun(d, module_loaded=True, mknod_fails=True)
agent.run = fr
try:
    agent.sw_ensure_loop_devices(st_dir=sd, loop_dir=d)  # trips degraded
finally:
    agent.run = agent_run
d2 = fresh_devdir(*loop_nodes(64))
fr2 = FakeRun(d2, module_loaded=True)
agent.run = fr2
try:
    if agent.sw_ensure_loop_devices(st_dir=sd, loop_dir=d2) is True:
        ok("G1 relief -> True")
    else:
        bad("G1 relief -> True")
    if not os.path.exists(os.path.join(sd, "loop.degraded")):
        ok("G2 degraded marker removed on relief")
    else:
        bad("G2 degraded marker removed on relief")
    if "relieved" in log_text(sd):
        ok("G3 relief logged")
    else:
        bad("G3 relief logged")
finally:
    agent.run = agent_run

# ---- H. never raises ---------------------------------------------------------
fr = FakeRun(fresh_devdir(), raise_all=True)
agent.run = fr
try:
    if agent.sw_ensure_loop_devices(st_dir=st_dir(), loop_dir=fresh_devdir()) is False:
        ok("H1 raising run -> False, never raises")
    else:
        bad("H1 raising run -> False, never raises")
finally:
    agent.run = agent_run
f = tempfile.NamedTemporaryFile(prefix="wld-stfile-", delete=False)
f.close()
try:
    if agent.sw_ensure_loop_devices(st_dir=f.name, loop_dir=fresh_devdir(*loop_nodes(64))) is True:
        ok("H2 corrupt state dir (a file) -> still True on healthy pool")
    else:
        bad("H2 corrupt state dir (a file) -> still True on healthy pool")
except Exception as e:
    bad("H2 corrupt state dir (a file) -> still True on healthy pool", "raised %r" % e)

# ---- I. selfwatch wiring (1.0.23: guards live in selfwatch_pass_once) --------
import inspect as _inspect
src = _inspect.getsource(agent.selfwatch_pass_once)
if "sw_ensure_loop_devices" in src and "for guard in" in src:
    body = src.split("for guard in")[1]
    if "sw_ensure_loop_devices" in body and "except Exception" in body:
        ok("I1 selfwatch_pass_once calls sw_ensure_loop_devices() in the host-level section, guarded")
    else:
        bad("I1 selfwatch_pass_once calls sw_ensure_loop_devices() in the host-level section, guarded")
else:
    bad("I1 selfwatch_pass_once calls sw_ensure_loop_devices() in the host-level section, guarded")
if "sw_ensure_loop_devices" in src and "for name in" in src.split("sw_ensure_loop_devices")[0]:
    ok("I2 loop guard runs once per pass (host-level), not per instance")
else:
    bad("I2 loop guard runs once per pass (host-level), not per instance")

# ---- J. create-time fail-open --------------------------------------------------
class _FakeDocker:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[:2] == ("network", "ls"):
            return 0, "__WING_NAME__\n"
        if args[:3] == ("network", "inspect", "__WING_NAME__"):
            return 0, "10.20.0.0/16 "
        if args[0] == "run":
            return 0, "container-id"
        return 0, ""


_real_docker = agent.docker
_real_load = agent.load_instances
_real_save = agent.save_instances
_real_container_status = agent.container_status
_real_swdir = agent.SELF_WATCH_DIR
_real_exists = os.path.exists
# fake binderfs devices (create_instance_linux checks the real /dev paths)
os.path.exists = lambda p: True if str(p).startswith("/dev/binderfs/") else _real_exists(p)
agent.SELF_WATCH_DIR = st_dir()  # hermetic: never touch the real selfwatch dir
agent.docker = _FakeDocker()
agent.load_instances = lambda: {}
agent.save_instances = lambda inst: True
agent.container_status = lambda cname: "missing"
_real_ensure = agent.sw_ensure_loop_devices
agent.sw_ensure_loop_devices = lambda *a, **k: False  # pool short, unfixable
try:
    ok2, msg = agent.create_instance_linux("srt9", 2, 2048, "10.20.0.5")
    if ok2 is True:
        ok("J1 short pool: create still succeeds (fail-open)")
    else:
        bad("J1 short pool: create still succeeds (fail-open)", "got %r %r" % (ok2, msg))
    if "loop device pool below floor at create time" in log_text(agent.SELF_WATCH_DIR):
        ok("J2 create-time shortfall logged as WARN")
    else:
        bad("J2 create-time shortfall logged as WARN")
finally:
    agent.docker = _real_docker
    agent.load_instances = _real_load
    agent.save_instances = _real_save
    agent.container_status = _real_container_status
    agent.SELF_WATCH_DIR = _real_swdir
    agent.sw_ensure_loop_devices = _real_ensure
    os.path.exists = _real_exists
agent.SELF_WATCH_DIR = st_dir()
agent.sw_ensure_loop_devices = lambda *a, **k: True  # healthy pool
agent.docker = _FakeDocker()
_real_exists = os.path.exists
os.path.exists = lambda p: True if str(p).startswith("/dev/binderfs/") else _real_exists(p)
agent.load_instances = lambda: {}
agent.save_instances = lambda inst: True
agent.container_status = lambda cname: "missing"
try:
    ok2, msg = agent.create_instance_linux("srt9", 2, 2048, "10.20.0.5")
    if ok2 is True:
        ok("J3 healthy pool: create succeeds with no WARN")
    else:
        bad("J3 healthy pool: create succeeds with no WARN", "got %r %r" % (ok2, msg))
    if "loop device pool below floor" not in log_text(agent.SELF_WATCH_DIR):
        ok("J4 healthy pool: no WARN logged")
    else:
        bad("J4 healthy pool: no WARN logged")
finally:
    agent.docker = _real_docker
    agent.load_instances = _real_load
    agent.save_instances = _real_save
    agent.container_status = _real_container_status
    agent.SELF_WATCH_DIR = _real_swdir
    agent.sw_ensure_loop_devices = _real_ensure
    os.path.exists = _real_exists

# ---- K. degraded visibility ----------------------------------------------------
sd = st_dir()
with open(os.path.join(sd, "loop.degraded"), "w") as fh:
    fh.write("loop device pool 8<60 after provisioning - needs human\n")
st = agent.selfwatch_state(st_dir=sd)
if st["degraded"].get("loop", "").startswith("loop device pool"):
    ok("K1 loop.degraded flows through selfwatch_state() degraded map")
else:
    bad("K1 loop.degraded flows through selfwatch_state() degraded map",
        "got %r" % (st["degraded"],))
with open(os.path.join(sd, "loop.degraded"), "w") as fh:
    fh.write("x" * 300 + "\n")
st = agent.selfwatch_state(st_dir=sd)
if len(st["degraded"].get("loop", "")) == 160:
    ok("K2 degraded reason truncated to 160 chars (dashboard-safe)")
else:
    bad("K2 degraded reason truncated to 160 chars (dashboard-safe)")

# ---- L. install-wing.sh contracts ----------------------------------------------
with open(INSTALL_SH) as fh:
    ish = fh.read()
start_marker = "# --- wing-loop-devices: install_loop_pool ---"
end_marker = "# --- /wing-loop-devices ---"
if ish.count(start_marker) == 1 and ish.count(end_marker) == 1:
    ok("L1 install_loop_pool delimited block present exactly once")
    fn_src = ish.split(start_marker)[1].split(end_marker)[0]
else:
    bad("L1 install_loop_pool delimited block present exactly once")
    fn_src = ""
if fn_src:
    if "def install_loop_pool" not in fn_src and "install_loop_pool() {" in fn_src:
        ok("L2 block defines install_loop_pool()")
    else:
        bad("L2 block defines install_loop_pool()")
    if "options loop max_loop=64" in fn_src and 'grep -qx "loop"' in fn_src:
        ok("L3 persistent config: modprobe.d loop.conf + /etc/modules")
    else:
        bad("L3 persistent config: modprobe.d loop.conf + /etc/modules")
    if "modprobe loop max_loop=64" in fn_src:
        ok("L4 immediate modprobe loop max_loop=64")
    else:
        bad("L4 immediate modprobe loop max_loop=64")
    if 'mknod "$dev_dir/loop$i" b 7 "$i"' in fn_src:
        ok("L5 mknod b 7 N for missing nodes")
    else:
        bad("L5 mknod b 7 N for missing nodes")
    if '[ "$n" -lt 60 ]' in fn_src and "WARNING" in fn_src:
        ok("L6 60-floor verify with WARNING")
    else:
        bad("L6 60-floor verify with WARNING")
    if "LP_MODPROBE_D" in fn_src and "LP_MODULES_FILE" in fn_src and "LP_DEV_DIR" in fn_src:
        ok("L7 LP_* path overrides for hermetic tests")
    else:
        bad("L7 LP_* path overrides for hermetic tests")
    if "'" not in fn_src:
        ok("L8 no apostrophes inside the single-quoted installer block (quote-break regression)")
    else:
        bad("L8 no apostrophes inside the single-quoted installer block (quote-break regression)",
            "found %d" % fn_src.count("'"))
    if "report " in fn_src:
        ok("L9 uses report() for install-progress visibility")
    else:
        bad("L9 uses report() for install-progress visibility")
# call site: after the binder mount, inside the installer body
m = re.search(r"mount -t binder binder /dev/binderfs.*\n(.*\n){0,3}install_loop_pool\n", ish)
if m:
    ok("L10 install_loop_pool called right after the binder setup")
else:
    bad("L10 install_loop_pool called right after the binder setup")

# ---- M. deploy-all bash -n gate --------------------------------------------------
with open(DEPLOY_ALL) as fh:
    da = fh.read()
if 'bash -n "$REPO/wing/install-wing.sh"' in da:
    ok("M1 deploy-all.sh syntax gate covers wing/install-wing.sh")
else:
    bad("M1 deploy-all.sh syntax gate covers wing/install-wing.sh")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
