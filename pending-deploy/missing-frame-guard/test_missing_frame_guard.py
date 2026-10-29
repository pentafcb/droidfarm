#!/usr/bin/env python3
"""Tests for missing-frame-guard (watchdog section 4b + agent 1.0.42 check 3).

GAP: both stale-stream repairs only handled a STALE live.png. When the
in-container screencap loop was alive but screencap failed every cycle,
live.png was never written at all — and the missing-file case was never
repaired: the main-node watchdog's section 4 only ran inside
`if [ -f live.png ]`, and the wing agent's selfwatch check 3 computed
age = -1 on the missing file, which fails its `age >= 30` gate. The
viewer then spun on MJPEG reconnects forever with no watchdog repair and
no DEGRADED badge, even though the loop process was alive.

FIX (both sides, same semantics):
  * missing live.png + booted marker + screencap loop alive + script
    present -> pkill + loop restart under the SHARED staleloop budget
    (3600s/10), log "screencap missing (no live.png)", recoveries bump.
  * booted gate: a first-boot instance legitimately has no frame yet.
  * dead loop is the helper check's job (and a loop restarted by the
    helper check this pass is skipped via flag, so one pass never
    restarts twice); a timed-out pgrep is UNKNOWN, never a restart.
  * missing script -> per-day WARN, no budget burn (same as stale).
  * budget trip -> DEGRADED marker, auto-recovery pauses (existing badge).

Covers:
  A. agent VERSION 1.0.42 + snapshot parity + manager gate compat
  B. agent selfwatch_check_instance: missing fires / not-booted skips /
     dead-loop single restart / helper-budget-out pgrep gate /
     missing-script WARN / degraded blocks / stale + fresh regressions /
     11-pass budget trip -> DEGRADED at exactly 10 restarts
  C. real df-watchdog.sh vs fake docker: same matrix (missing fires /
     not-booted skips / dead-loop single restart via flag / missing
     script WARN / pgrep-timeout skip / stale regression / 11-run
     budget trip)
  D. source contracts: shared staleloop keys, gates, no destructive
     commands in the new blocks
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")
WD = os.path.join(REPO, "waydroid-image", "df-watchdog.sh")
SNAP = os.path.join(REPO, "pending-deploy", "remote-autologin", "agent.py")

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


agent = load("mfg_agent", AGENT)
app = load("mfg_app", APP)
wd_src = open(WD, encoding="utf-8").read()
ag_src = open(AGENT, encoding="utf-8").read()

print("== A. versions ==")
if agent.VERSION == "1.0.45":
    ok("agent VERSION is 1.0.44")
else:
    bad("agent VERSION is 1.0.44", repr(agent.VERSION))
snap = load("mfg_snap", SNAP)
if snap.VERSION == agent.VERSION:
    ok("remote-autologin agent.py snapshot matches canonical VERSION")
else:
    bad("remote-autologin snapshot VERSION", repr(snap.VERSION))
if "1.0.41 focus gate" in ag_src:
    ok("historical 1.0.41 focus-gate comment untouched")
else:
    bad("historical 1.0.41 focus-gate comment untouched")
if app._version_at_least("1.0.42", "1.0.2"):
    ok("manager version gate accepts 1.0.42")
else:
    bad("manager version gate accepts 1.0.42")


# ---------------------------------------------------------------- B. agent
class FakeDocker:
    """Stand-in for agent.docker: records calls, simulates container state."""

    def __init__(self):
        self.calls = []
        self.containers = {}
        self.helpers = {}
        self.session_alive = True
        self.restart_lands = True

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[0] == "ps":
            return 0, "\n".join(self.containers.keys())
        if args[0] == "exec":
            # NOTE: the detached form puts "-d" BEFORE the container name
            # (docker("exec", "-d", cname, ...)), so membership tests must
            # run over the whole args tuple, not over args[2:].
            cname = args[2] if len(args) > 1 and args[1] == "-d" else args[1]
            if "getprop" in args:
                return (0, "1\n") if self.session_alive else (1, "")
            if "dumpsys" in args:
                return (0, "Package [com.roblox.client]:\n"
                           "  versionName=2.740.931\n")
            if "pgrep" in args:
                script = args[-1]
                return (0, "4242\n") if self.helpers.get((cname, script)) else (1, "")
            if "pkill" in args:
                self.helpers[(cname, args[-1])] = False
                return 0, ""
            if "-d" in args:
                # detached helper restart: lands -> helper alive again
                for s in ("screencap-loop.sh", "input-daemon.sh",
                          "roblox-keepalive.sh"):
                    if s in " ".join(args):
                        if self.restart_lands:
                            self.helpers[(cname, s)] = True
                return 0, ""
            return 0, ""
        if args[0] == "start":
            self.containers[args[1]] = True
            return 0, ""
        return 0, ""

    def restarts(self, script="screencap-loop.sh"):
        return [c for c in self.calls
                if c[0] == "exec" and "-d" in c and script in " ".join(c)]

    def pkills(self, script="screencap-loop.sh"):
        return [c for c in self.calls
                if c[0] == "exec" and "pkill" in c and script in c[-1]]


def make_env(booted=True, png_age=None, loop_alive=True,
             scripts=True, restart_lands=True):
    st = tempfile.mkdtemp()
    vol = tempfile.mkdtemp()
    fake = FakeDocker()
    fake.containers["wd-srt1"] = True
    fake.restart_lands = restart_lands
    for s in ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh"):
        fake.helpers[("wd-srt1", s)] = (loop_alive if s == "screencap-loop.sh"
                                        else True)
    d = os.path.join(vol, "wd-srt1-data", "_data", "data", "media",
                     "0", "jerusalem")
    os.makedirs(d, exist_ok=True)
    if booted:
        open(os.path.join(d, "booted"), "w").write("1\n")
    if scripts:
        for s in ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh"):
            open(os.path.join(d, s), "w").write("#!/bin/sh\n")
    if png_age is not None:
        png = os.path.join(d, "live.png")
        open(png, "w").write("x")
        ts = time.time() - png_age
        os.utime(png, (ts, ts))
    return st, vol, fake


orig_docker = agent.docker


def use_fake(fake):
    agent.docker = fake


def restore():
    agent.docker = orig_docker


def read_log(st):
    p = os.path.join(st, "selfwatch.log")
    try:
        return open(p).read()
    except Exception:
        return ""


def recoveries(st):
    import json
    try:
        return json.load(open(os.path.join(st, "recoveries.json")))["total"]
    except Exception:
        return 0


print("== B. agent missing-frame behavior ==")
st, vol, fake = make_env()
use_fake(fake)
try:
    r = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
if (r is False and fake.pkills() and fake.restarts()
        and "screencap missing (no live.png)" in read_log(st)
        and recoveries(st) == 1):
    ok("B1 missing+booted+loop alive -> pkill + restart + log + count")
else:
    bad("B1 missing+booted+loop alive", f"r={r} pk={fake.pkills()} "
        f"rl={fake.restarts()} rec={recoveries(st)}")

st, vol, fake = make_env(booted=False)
use_fake(fake)
try:
    agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
if not fake.restarts() and not fake.pkills() \
        and "screencap missing" not in read_log(st):
    ok("B2 missing+not booted -> no restart, no log")
else:
    bad("B2 missing+not booted", f"rl={fake.restarts()} pk={fake.pkills()}")

st, vol, fake = make_env(loop_alive=False, restart_lands=True)
use_fake(fake)
try:
    agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
if len(fake.restarts()) == 1 and not fake.pkills() \
        and "screencap missing" not in read_log(st):
    ok("B3 missing+loop dead+restart lands -> exactly one restart (check 2)")
else:
    bad("B3 missing+loop dead", f"rl={fake.restarts()} pk={fake.pkills()}")

st, vol, fake = make_env(loop_alive=False, restart_lands=False)
# Exhaust the helper budget first (8 events in 600s): check 2's cb_guard
# then refuses the restart, so loop_restarted stays False and check 3
# must skip on its own pgrep gate — the true pgrep-gate path.
now = int(time.time())
with open(os.path.join(st, "srt1.helper-screencap_loop_sh.ev"), "w") as fh:
    fh.write("\n".join(str(now) for _ in range(8)) + "\n")
use_fake(fake)
try:
    agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
if not fake.restarts() and not fake.pkills() \
        and "screencap missing" not in read_log(st):
    ok("B4 missing+loop dead+helper budget out -> no restart (pgrep gate)")
else:
    bad("B4 missing+loop dead+helper budget out", f"rl={fake.restarts()} "
        f"pk={fake.pkills()}")

st, vol, fake = make_env(scripts=False)
use_fake(fake)
try:
    agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
ev = os.path.join(st, "srt1.staleloop.ev")
if (not fake.restarts() and not fake.pkills()
        and "WARN: screencap-loop.sh missing" in read_log(st)
        and not os.path.exists(ev)):
    ok("B5 missing+script missing -> WARN, no restart, no budget burn")
else:
    bad("B5 missing+script missing", f"rl={fake.restarts()} ev={os.path.exists(ev)}")

st, vol, fake = make_env()
open(os.path.join(st, "srt1.degraded"), "w").write("prior trip\n")
use_fake(fake)
try:
    agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
if not fake.restarts() and not fake.pkills() \
        and "screencap missing" not in read_log(st):
    ok("B6 missing+already DEGRADED -> cb_guard blocks, no restart")
else:
    bad("B6 missing+already DEGRADED", f"rl={fake.restarts()}")

st, vol, fake = make_env(png_age=65)
use_fake(fake)
try:
    r = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
if r is False and fake.pkills() and fake.restarts() \
        and "screencap stale (" in read_log(st):
    ok("B7 stale regression: stale png still pkill + restart + log")
else:
    bad("B7 stale regression", f"r={r} pk={fake.pkills()}")

st, vol, fake = make_env(png_age=5)
use_fake(fake)
try:
    r = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
if r is True and not fake.pkills() and not fake.restarts():
    ok("B8 fresh regression: fresh png -> no action, healthy")
else:
    bad("B8 fresh regression", f"r={r}")

st, vol, fake = make_env()
use_fake(fake)
try:
    for _ in range(11):
        agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
deg = os.path.join(st, "srt1.degraded")
if (len(fake.restarts()) == 10 and os.path.exists(deg)
        and "screencap-loop" in open(deg).read()
        and recoveries(st) == 10):
    ok("B9 11 passes -> exactly 10 restarts, then DEGRADED (shared budget)")
else:
    bad("B9 budget trip", f"rl={len(fake.restarts())} deg={os.path.exists(deg)} "
        f"rec={recoveries(st)}")


# ---------------------------------------------------------------- C. watchdog
FAKE_DOCKER = r"""#!/bin/bash
echo "docker $*" >> "$T/calls"
sub=$1; shift
case "$sub" in
  ps)      cat "$T/docker_ps" 2>/dev/null ;;
  inspect) cat "$T/docker_started_at" 2>/dev/null ;;
  start|restart) exit 0 ;;
  exec)
    cname=$1; shift
    case "$1" in
      pgrep)
        [ -f "$T/pgrep_timeout" ] && exit 124
        grep -qx "$3" "$T/pgrep_dead" 2>/dev/null && exit 1 || exit 0 ;;
      pkill) exit 0 ;;
      waydroid)
        case "$*" in
          *getprop*) echo 1; exit 0 ;;
          *dumpsys*) printf 'Package [com.roblox.client]:\n  versionName=2.740.931\n'; exit 0 ;;
        esac
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
"""
FAKE_DF = r"""#!/bin/bash
printf 'Filesystem     1K-blocks      Used Available Use%% Mounted on\n/dev/sda1      100000000  92000000   8000000  70%% /var/lib/docker\n'
"""
FAKE_CURL = "#!/bin/bash\necho 200; exit 0\n"
FAKE_SYSTEMCTL = "#!/bin/bash\nexit 0\n"


class Rig:
    def __init__(self, t):
        self.t = t
        self.bin = os.path.join(t, "bin")
        self.st = os.path.join(t, "st")
        self.vol = os.path.join(t, "vol")
        self.log = os.path.join(t, "watchdog.log")
        os.makedirs(self.bin)
        for name, body in (("docker", FAKE_DOCKER), ("df", FAKE_DF),
                           ("curl", FAKE_CURL), ("systemctl", FAKE_SYSTEMCTL)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)

    def setup(self, booted=True, png_age=None, loop_dead=False,
              scripts=True, pgrep_timeout=False):
        for d in (self.st, self.vol):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d)
        open(self.log, "w").close()
        for f in ("docker_ps", "docker_started_at", "pgrep_dead",
                  "pgrep_timeout", "calls"):
            p = os.path.join(self.t, f)
            if os.path.exists(p):
                os.remove(p)
        with open(os.path.join(self.t, "docker_ps"), "w") as f:
            f.write("wd-rbx1\n")
        if loop_dead:
            with open(os.path.join(self.t, "pgrep_dead"), "w") as f:
                f.write("screencap-loop.sh\n")
        if pgrep_timeout:
            open(os.path.join(self.t, "pgrep_timeout"), "w").close()
        d = os.path.join(self.vol, "wd-rbx1-data", "_data", "data", "media",
                         "0", "jerusalem")
        os.makedirs(d, exist_ok=True)
        if booted:
            open(os.path.join(d, "booted"), "w").write("1\n")
        if scripts:
            for s in ("screencap-loop.sh", "input-daemon.sh",
                      "roblox-keepalive.sh"):
                open(os.path.join(d, s), "w").write("#!/bin/sh\n")
        if png_age is not None:
            png = os.path.join(d, "live.png")
            open(png, "w").write("x")
            ts = time.time() - png_age
            os.utime(png, (ts, ts))

    def run(self):
        env = dict(os.environ, DF_ST=self.st, DF_LOG=self.log,
                   DF_INSTANCES="rbx1", DF_VOLBASE=self.vol,
                   DF_PINNED_ROBLOX="2.740.931", T=self.t,
                   PATH=self.bin + ":/usr/bin:/bin")
        subprocess.run(["bash", WD], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # backgrounded docker restarts record their call asynchronously
        for _ in range(20):
            if os.path.exists(os.path.join(self.t, "calls")):
                break
            time.sleep(0.1)
        time.sleep(0.2)

    def read_log(self):
        with open(self.log) as f:
            return f.read()

    def calls(self):
        p = os.path.join(self.t, "calls")
        try:
            return open(p).read()
        except Exception:
            return ""

    def screencap_restarts(self):
        return [ln for ln in self.calls().splitlines()
                if "nohup waydroid shell sh" in ln
                and "screencap-loop.sh" in ln]


print("== C. watchdog missing-frame behavior ==")
with tempfile.TemporaryDirectory() as t:
    rig = Rig(t)
    rig.setup()
    rig.run()
    if ("screencap missing (no live.png)" in rig.read_log()
            and len(rig.screencap_restarts()) == 1
            and os.path.exists(os.path.join(rig.st, "rbx1.staleloop.ev"))):
        ok("C1 missing+booted+loop alive -> restart + log + budget event")
    else:
        bad("C1 missing+booted+loop alive", rig.read_log()[-300:])

    rig.setup(booted=False)
    rig.run()
    if "screencap missing" not in rig.read_log() \
            and not rig.screencap_restarts():
        ok("C2 missing+not booted -> no restart, no log")
    else:
        bad("C2 missing+not booted", rig.read_log()[-300:])

    rig.setup(loop_dead=True)
    rig.run()
    if (len(rig.screencap_restarts()) == 1
            and "screencap missing" not in rig.read_log()):
        ok("C3 missing+loop dead -> exactly one restart (section 3, flag gate)")
    else:
        bad("C3 missing+loop dead", f"restarts={rig.screencap_restarts()}")

    rig.setup(scripts=False)
    rig.run()
    if ("WARN: screencap-loop.sh missing" in rig.read_log()
            and not rig.screencap_restarts()
            and not os.path.exists(os.path.join(rig.st, "rbx1.staleloop.ev"))):
        ok("C4 missing+script missing -> WARN, no restart, no budget burn")
    else:
        bad("C4 missing+script missing", rig.read_log()[-300:])

    rig.setup(pgrep_timeout=True)
    rig.run()
    if (not rig.screencap_restarts()
            and not os.path.exists(os.path.join(rig.st, "rbx1.staleloop.ev"))):
        ok("C5 missing+pgrep timeout -> UNKNOWN, no restart, no budget burn")
    else:
        bad("C5 missing+pgrep timeout", f"restarts={rig.screencap_restarts()}")

    rig.setup(png_age=65)
    rig.run()
    if ("screencap stale (" in rig.read_log()
            and len(rig.screencap_restarts()) == 1):
        ok("C6 stale regression: stale png still restart + log")
    else:
        bad("C6 stale regression", rig.read_log()[-300:])

    rig.setup()
    for _ in range(11):
        rig.run()
    deg = os.path.join(rig.st, "rbx1.degraded")
    if (len(rig.screencap_restarts()) == 10 and os.path.exists(deg)
            and "screencap-loop" in open(deg).read()):
        ok("C7 11 runs -> exactly 10 restarts, then DEGRADED (shared budget)")
    else:
        bad("C7 budget trip", f"restarts={len(rig.screencap_restarts())} "
            f"deg={os.path.exists(deg)}")


# ---------------------------------------------------------------- D. contracts
print("== D. source contracts ==")
if ag_src.count('name + ".staleloop"') == 2:
    ok("agent stale + missing share one staleloop cb key (2 call sites)")
else:
    bad("agent staleloop key count", ag_src.count('name + ".staleloop"'))
if wd_src.count('"$i.staleloop"') == 2:
    ok("watchdog stale + missing share one staleloop cb key (2 call sites)")
else:
    bad("watchdog staleloop key count", wd_src.count('"$i.staleloop"'))
if "_loop_restarted=1" in wd_src and '[ "$s" = "screencap-loop.sh" ]' in wd_src:
    ok("watchdog section 3 sets _loop_restarted for screencap-loop.sh")
else:
    bad("watchdog _loop_restarted flag")
if "loop_restarted = False" in ag_src \
        and 'if script == "screencap-loop.sh":' in ag_src:
    ok("agent check 2 sets loop_restarted for screencap-loop.sh")
else:
    bad("agent loop_restarted flag")
if "sw_booted_once(name, vol_base)" in ag_src:
    ok("agent missing branch gated on sw_booted_once")
else:
    bad("agent booted gate")
if '[ -f "$base/booted" ]' in wd_src:
    ok("watchdog missing branch gated on booted marker")
else:
    bad("watchdog booted gate")
m = re.search(r"# 4b\. frame MISSING entirely.*?(?=\n  # 5\. )", wd_src, re.S)
if m and "docker rm" not in m.group(0) and "prune" not in m.group(0) \
        and "volume" not in m.group(0).lower().replace("data volume", ""):
    ok("watchdog 4b block: no destructive commands")
else:
    bad("watchdog 4b block safety", bool(m))

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
