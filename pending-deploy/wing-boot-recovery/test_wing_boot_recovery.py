#!/usr/bin/env python3
"""Tests for wing agent 1.0.32 boot recovery (df-watchdog.sh v14 section-0 parity).

REAL GAP: the wing agent's selfwatch had the same post-host-reboot gap the
main node's df-watchdog v14 closed — a reboot stops every wd-* container
(docker run sets no --restart policy) and drops the binderfs pool back to the
stock set (only the CREATE path ever restored it, agent 1.0.16). The
selfwatch container-down check then fired for EVERY stopped instance in the
same pass: a Waydroid boot storm on the wing box, and `docker start` on
containers whose bind-mounted /dev/binderfs/binderN sources had vanished
(docker auto-creates empty dirs at missing bind sources -> dead binder
mounts -> stuck-boot wedge -> 6/hour container-start budget burned into mass
DEGRADED).

Agent 1.0.32 adds sw_boot_recovery() as the first step of every held
selfwatch pass: (1) binder pool restore (mount if needed + binder-control
writes; empty docker stub dirs rmdir'd — rmdir removes ONLY empty dirs so it
can never delete data — and recreated; non-empty stubs left alone with one
ALERT per boot and the fleet start paused), (2) staggered starts
(SW_BOOT_MAX_STARTS docker starts per 60s pass, sorted, .degraded skipped,
per-instance SW_BOOT_ATTEMPTS budget before DEGRADED with reason on the
marker's first line -> selfwatch_state() degraded map -> heartbeat ->
dashboard Nodes "needs attention" badge, zero manager/dashboard changes),
(3) sentinel written when the queue drains. While active,
selfwatch_check_instance's container-down branch defers its own starts (no
race, no herd). Missed window (uptime >= window): sentinel once, normal
checks own recovery. Unreadable uptime: fail open, retry next cycle.
Linux only (selfwatch itself is Linux-only); never raises; never deletes
instance data, credentials, or tokens; never stops/kills/restarts anything
except `docker start` on stopped containers.

This suite covers:

  A. agent.VERSION is exactly 1.0.32 + parse_agent_version ordering
  B. _sw_read_uptime_s: normal / missing / garbage / empty -> int or None
  C. sw_boot_recovery_active: sentinel / uptime / unreadable / window seam
  D. noop-sentinel: sentinel present -> no docker, no pool work
  E. unreadable uptime -> "unreadable", no sentinel write
  F. missed window -> "noop-window", sentinel written, docker untouched
  G. fresh boot, pool complete, fleet already running -> "complete"
  H. stagger: 5 down, 3/pass -> "progress", exactly 3 starts, sorted order,
     no sentinel yet
  I. drain: remaining down instances -> "complete", sentinel written
  J. failed starts stay queued ("progress"), retry next pass, no sentinel
  K. budget: >SW_BOOT_ATTEMPTS failed starts -> DEGRADED marker (reason on
     first line), surfaces in selfwatch_state(), skipped next pass
  L. pre-degraded instance skipped (no start attempt)
  M. pool restore: stock set -> exactly the missing names created, count 64
  N. empty stub dir rmdir'd (empty-only) and recreated via binder-control
  O. non-empty stub dir -> stub_blocked, ONE alert per boot, "pool-paused",
     no docker start
  P. pool incomplete (writes rejected) -> "incomplete", no sentinel
  Q. docker ps failure -> "incomplete", no sentinel
  R. deferral: check_instance with boot_recovery="progress" defers the
     2-strike start (log line); "complete" starts normally
  S. binder-control missing: modprobe+mount attempted; mount failure ->
     "pool-paused"
  T. env sanitize: 0/negative/garbage knobs fall back to defaults
  U. invalid instance names never reach docker
  V. never-raises: raising docker_fn -> "error"; raising run_fn -> degraded
     path handled, no raise
  W. wiring: selfwatch_pass_once runs sw_boot_recovery first and passes
     boot_recovery= into selfwatch_check_instance; check_instance honors it
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


agent = load("wbr_agent", AGENT)


class FakeDocker:
    """Stand-in for agent.docker: (rc, out) shape, records calls."""

    def __init__(self, running=(), start_rc=0, ps_rc=0):
        self.calls = []
        self.running = set(running)   # full "wd-name|state" lines for ps -a
        self.start_rc = start_rc
        self.ps_rc = ps_rc
        self.started = []

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[0] == "ps":
            if self.ps_rc != 0:
                return self.ps_rc, "Cannot connect to the Docker daemon"
            return 0, "\n".join(sorted(self.running))
        if args[0] == "start":
            if self.start_rc != 0:
                return self.start_rc, "boom"
            self.started.append(args[1])
            return 0, args[1] + "\n"
        return 0, ""

    def starts(self):
        return [c for c in self.calls if c[0] == "start"]


class FakeRun:
    """Stand-in for agent.run(cmd, timeout=30, shell=True) against a fake
    /dev/binderfs: real directories/files, shell side-effects simulated."""

    def __init__(self, binderfs, control_ok=True, mount_ok=True,
                 create_ok=True):
        self.binderfs = binderfs
        self.mount_ok = mount_ok
        self.create_ok = create_ok
        self.cmds = []
        if control_ok:
            open(os.path.join(binderfs, "binder-control"), "w").close()

    def __call__(self, cmd, timeout=30, shell=False):
        self.cmds.append(cmd)
        c = cmd.replace(" 2>/dev/null", "")
        if c.startswith("modprobe binder_linux"):
            return 0, ""
        if c.startswith("mkdir -p ") and "mount -t binder" in c:
            if self.mount_ok:
                open(os.path.join(self.binderfs, "binder-control"), "w").close()
                return 0, ""
            return 1, "mount failed"
        m = re.fullmatch(r"echo (\S+) > (\S+)", c)
        if m:
            name, ctl = m.group(1), m.group(2)
            if not self.create_ok:
                return 1, "rejected"
            # kernel creates the device on an accepted write
            open(os.path.join(self.binderfs, name), "w").close()
            return 0, ""
        return 0, ""


def make_env(uptime_s=300.0, stock=8):
    """Fresh hermetic env: (st, sentinel_dir, uptime_path, binderfs, fake_run).

    stock = number of pre-existing binder devices (binder, binder1..).
    """
    st = tempfile.mkdtemp()
    sentinel_dir = tempfile.mkdtemp()
    b = tempfile.mkdtemp()
    open(os.path.join(b, "binder-control"), "w").close()
    for i in range(stock):
        open(os.path.join(b, "binder" if i == 0 else "binder%d" % i), "w").close()
    up = os.path.join(tempfile.mkdtemp(), "uptime")
    with open(up, "w") as fh:
        fh.write("%.2f 12345.00\n" % uptime_s)
    return st, sentinel_dir, up, b, FakeRun(b)


def read_log(st):
    try:
        return open(os.path.join(st, "selfwatch.log")).read()
    except Exception:
        return ""


def sentinel(st_dir_sentinel):
    return os.path.join(st_dir_sentinel, "wing-boot.done")


print("== A. version bump + gate compat ==")
check(agent.VERSION in ("1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"), "A1 agent VERSION is 1.0.35+",
      repr(agent.VERSION))
check(agent.parse_agent_version("1.0.35") > agent.parse_agent_version("1.0.34"),
      "A2 parse_agent_version ordering: 1.0.35 > 1.0.34")
check(hasattr(agent, "sw_boot_recovery") and hasattr(agent, "sw_boot_recovery_active")
      and hasattr(agent, "sw_boot_binder_pool"),
      "A3 the three boot-recovery entry points exist")

print("== B. uptime reader ==")
up_ok = os.path.join(tempfile.mkdtemp(), "uptime")
open(up_ok, "w").write("300.75 9999.00\n")
check(agent._sw_read_uptime_s(up_ok) == 300, "B1 normal uptime parses to 300")
check(agent._sw_read_uptime_s("/nonexistent/uptime") is None,
      "B2 missing file -> None (fail open)")
up_bad = os.path.join(tempfile.mkdtemp(), "uptime")
open(up_bad, "w").write("garbage\n")
check(agent._sw_read_uptime_s(up_bad) is None, "B3 garbage -> None")
up_empty = os.path.join(tempfile.mkdtemp(), "uptime")
open(up_empty, "w").write("")
check(agent._sw_read_uptime_s(up_empty) is None, "B4 empty -> None")

print("== C. recovery-active seam ==")
st, sd, up, b, fr = make_env(uptime_s=300.0)
check(agent.sw_boot_recovery_active(sentinel_dir=sd, uptime_path=up, window_s=900),
      "C1 fresh boot (300s < 900s, no sentinel) -> active")
check(not agent.sw_boot_recovery_active(sentinel_dir=sd, uptime_path=up, window_s=200),
      "C2 window seam respected (300s >= 200s) -> inactive")
st2, sd2, up2, b2, fr2 = make_env(uptime_s=5000.0)
check(not agent.sw_boot_recovery_active(sentinel_dir=sd2, uptime_path=up2, window_s=900),
      "C3 old boot (5000s) -> inactive")
open(sentinel(sd), "w").close()
check(not agent.sw_boot_recovery_active(sentinel_dir=sd, uptime_path=up, window_s=900),
      "C4 sentinel present -> inactive even on fresh boot")
check(not agent.sw_boot_recovery_active(sentinel_dir=sd2, uptime_path="/nonexistent",
                                       window_s=900),
      "C5 unreadable uptime -> inactive (fail open, no storm)")
check(isinstance(agent.sw_boot_recovery_active(sentinel_dir=None, uptime_path=None), bool),
      "C6 returns a bool on garbage/defaults (never raises)")

print("== D. noop-sentinel ==")
st, sd, up, b, fr = make_env(uptime_s=300.0)
open(sentinel(sd), "w").close()
fd = FakeDocker(running=())
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                           binderfs=b, instances=["srt1"],
                           docker_fn=fd, run_fn=fr)
check(r == "noop-sentinel", "D1 sentinel present -> noop-sentinel", repr(r))
check(fd.calls == [] and fr.cmds == [],
      "D2 no docker calls, no pool writes on noop-sentinel")

print("== E. unreadable uptime ==")
st, sd, up, b, fr = make_env(uptime_s=300.0)
fd = FakeDocker(running=())
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd,
                           uptime_path="/nonexistent/uptime",
                           binderfs=b, instances=["srt1"],
                           docker_fn=fd, run_fn=fr)
check(r == "unreadable", "E1 unreadable uptime -> unreadable", repr(r))
check(not os.path.exists(sentinel(sd)),
      "E2 no sentinel written (retry next pass)")
check(fd.calls == [], "E3 no docker touched")

print("== F. missed window ==")
st, sd, up, b, fr = make_env(uptime_s=5000.0)
fd = FakeDocker(running=())
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                           binderfs=b, instances=["srt1"],
                           docker_fn=fd, run_fn=fr)
check(r == "noop-window", "F1 uptime >= window -> noop-window", repr(r))
check(os.path.exists(sentinel(sd)), "F2 sentinel written once")
check(fd.calls == [], "F3 normal checks own recovery: docker untouched")
check("missed" in read_log(st), "F4 missed-window log line")

print("== G. fresh boot, fleet already running ==")
st, sd, up, b, fr = make_env(uptime_s=300.0)
fd = FakeDocker(running=("wd-srt1|running", "wd-srt2|running"))
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                           binderfs=b, instances=["srt1", "srt2"],
                           docker_fn=fd, run_fn=fr)
check(r == "complete", "G1 nothing down -> complete", repr(r))
check(os.path.exists(sentinel(sd)), "G2 sentinel written")
check(fd.starts() == [], "G3 no starts when fleet is running")
check("binder pool at 64/64" in read_log(st), "G4 pool-restore log line")

print("== H. staggered starts ==")
st, sd, up, b, fr = make_env(uptime_s=300.0)
fd = FakeDocker(running=())
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                           binderfs=b,
                           instances=["srt1", "srt2", "srt3", "srt4", "srt5"],
                           docker_fn=fd, run_fn=fr)
check(r == "progress", "H1 5 down, budget 3 -> progress", repr(r))
check(fd.starts() == [("start", "wd-srt1"), ("start", "wd-srt2"),
                      ("start", "wd-srt3")],
      "H2 exactly 3 starts this pass, sorted order",
      repr([c[1] for c in fd.starts()]))
check(not os.path.exists(sentinel(sd)), "H3 no sentinel while queued")
ev1 = open(os.path.join(st, "srt1.bootstart.ev")).read().strip().split("\n")
check(len(ev1) == 1 and ev1[0].isdigit(), "H4 per-instance .bootstart event noted")
try:
    tot = __import__("json").load(open(os.path.join(st, "recoveries.json")))["total"]
except Exception:
    tot = -1
check(tot == 3, "H5 recoveries.json bumped once per started container", repr(tot))

print("== I. queue drains -> complete ==")
fd.running = set(["wd-srt%d|running" % i for i in (1, 2, 3)])
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                           binderfs=b, instances=["srt1", "srt2", "srt3", "srt4", "srt5"],
                           docker_fn=fd, run_fn=fr)
check(r == "complete", "I1 remaining 2 start -> complete", repr(r))
check(os.path.exists(sentinel(sd)), "I2 sentinel written on drain")
check("boot recovery complete" in read_log(st), "I3 completion log line")

print("== J. failed starts stay queued ==")
st, sd, up, b, fr = make_env(uptime_s=300.0)
fd = FakeDocker(running=(), start_rc=1)
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                           binderfs=b, instances=["srt1"],
                           docker_fn=fd, run_fn=fr)
check(r == "progress", "J1 all starts fail -> progress (retry next pass)", repr(r))
check(not os.path.exists(sentinel(sd)), "J2 no sentinel on failure")
check("FAILED" in read_log(st), "J3 failure log line")
check(not os.path.exists(os.path.join(st, "srt1.degraded")),
      "J4 under budget -> no DEGRADED marker yet")

print("== K. attempt budget -> DEGRADED ==")
st, sd, up, b, fr = make_env(uptime_s=300.0)
fd = FakeDocker(running=(), start_rc=1)
statuses = []
for _ in range(4):
    statuses.append(agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd,
                                           uptime_path=up, binderfs=b,
                                           instances=["srt1"],
                                           docker_fn=fd, run_fn=fr))
check(statuses[:3] == ["progress"] * 3 and statuses[3] == "complete",
      "K1 3 failed passes progress, 4th degrades and drains -> complete",
      repr(statuses))
mp = os.path.join(st, "srt1.degraded")
check(os.path.isfile(mp), "K2 DEGRADED marker written after >3 attempts")
try:
    reason = open(mp).readline().strip()
except Exception:
    reason = ""
check("boot recovery" in reason and "auto-recovery paused" in reason,
      "K3 reason on first line (dashboard contract)", repr(reason[:80]))
deg, dreason = agent.read_selfwatch_degraded("srt1", st_dir=st)
check(deg and dreason and "boot recovery" in dreason,
      "K4 surfaces via read_selfwatch_degraded")
state = agent.selfwatch_state(st_dir=st)
check(state["degraded"].get("srt1", "").startswith("failed to start"),
      "K5 surfaces via selfwatch_state() degraded map -> heartbeat")
# next pass skips the degraded instance entirely (sentinel already written)
fd2 = FakeDocker(running=(), start_rc=1)
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                           binderfs=b, instances=["srt1"],
                           docker_fn=fd2, run_fn=fr)
check(r == "noop-sentinel" and fd2.starts() == [],
      "K6 post-drain pass is a no-op (no start attempt)")

print("== L. pre-degraded instance skipped ==")
st, sd, up, b, fr = make_env(uptime_s=300.0)
open(os.path.join(st, "srt9.degraded"), "w").write("old circuit trip\n")
fd = FakeDocker(running=())
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                           binderfs=b, instances=["srt9"],
                           docker_fn=fd, run_fn=fr)
check(fd.starts() == [], "L1 no start attempt on .degraded instance")
check(r == "complete", "L2 skipped instance drains the queue", repr(r))

print("== M. pool restore creates exactly the missing names ==")
st, sd, up, b, fr = make_env(uptime_s=300.0, stock=8)
have, blocked = agent.sw_boot_binder_pool(st_dir=st, binderfs=b, run_fn=fr,
                                          alert_dir=sd)
check((have, blocked) == (64, False), "M1 8 stock -> 64 devices, not blocked",
      repr((have, blocked)))
writes = [c for c in fr.cmds if c.startswith("echo binder")]
check(len(writes) == 56, "M2 exactly 56 creation writes", repr(len(writes)))
names = sorted(re.fullmatch(r"echo (\S+) > .*", c).group(1) for c in writes)
check(set(names) == set("binder%d" % i for i in range(8, 64)),
      "M3 created names are binder8..binder63", repr(names[:3]))
check(all(re.fullmatch(r"[A-Za-z0-9]+", n) for n in names),
      "M4 generated names contain no shell metachars")

print("== N. empty stub dir rmdir'd and recreated ==")
st, sd, up, b, fr = make_env(uptime_s=300.0, stock=8)
os.remove(os.path.join(b, "binder5"))
os.mkdir(os.path.join(b, "binder5"))  # empty dir = docker bind stub
have, blocked = agent.sw_boot_binder_pool(st_dir=st, binderfs=b, run_fn=fr,
                                          alert_dir=sd)
check((have, blocked) == (64, False), "N1 stub rmdir'd, pool complete",
      repr((have, blocked)))
check(any("binder5" in c for c in fr.cmds if c.startswith("echo binder")),
      "N2 binder5 recreated via binder-control")
check("bind-stub dir binder5" in read_log(st), "N3 stub-removal log line")

print("== O. non-empty stub dir blocks with one alert ==")
st, sd, up, b, fr = make_env(uptime_s=300.0, stock=8)
os.remove(os.path.join(b, "binder5"))
os.mkdir(os.path.join(b, "binder5"))
open(os.path.join(b, "binder5", "junk"), "w").write("x")  # non-empty
have, blocked = agent.sw_boot_binder_pool(st_dir=st, binderfs=b, run_fn=fr,
                                          alert_dir=sd)
check(blocked, "O1 non-empty stub -> stub_blocked=True")
check(have == 63, "O2 blocked name skipped in the count", repr(have))
check(os.path.isdir(os.path.join(b, "binder5")),
      "O3 non-empty stub dir KEPT (never deletes data)")
log1 = read_log(st)
check(log1.count("ALERT boot recovery") == 1, "O4 exactly one ALERT", repr(log1.count("ALERT")))
have2, _ = agent.sw_boot_binder_pool(st_dir=st, binderfs=b, run_fn=fr,
                                     alert_dir=sd)
check(read_log(st).count("ALERT boot recovery") == 1,
      "O5 alert deduped per boot (no spam)")
fd = FakeDocker(running=())
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                           binderfs=b, instances=["srt1"],
                           docker_fn=fd, run_fn=fr)
check(r == "pool-paused", "O6 stub block -> pool-paused", repr(r))
check(fd.calls == [], "O7 fleet start paused: no docker start")

print("== P. pool incomplete (writes rejected) ==")
st, sd, up, b, fr = make_env(uptime_s=300.0, stock=8)
fr.create_ok = False
fd = FakeDocker(running=())
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                           binderfs=b, instances=["srt1"],
                           docker_fn=fd, run_fn=fr)
check(r == "incomplete", "P1 rejected writes -> incomplete", repr(r))
check(not os.path.exists(sentinel(sd)), "P2 no sentinel (retry next pass)")
check(fd.calls == [], "P3 fleet start paused")

print("== Q. docker ps failure ==")
st, sd, up, b, fr = make_env(uptime_s=300.0)
fd = FakeDocker(running=(), ps_rc=1)
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                           binderfs=b, instances=["srt1"],
                           docker_fn=fd, run_fn=fr)
check(r == "incomplete", "Q1 docker ps -a failure -> incomplete", repr(r))
check(not os.path.exists(sentinel(sd)), "Q2 no sentinel on docker failure")

print("== R. check_instance deferral ==")
orig_docker = agent.docker
try:
    st, sd, up, b, fr = make_env(uptime_s=300.0)
    fd = FakeDocker(running=())  # srt1 down
    agent.docker = fd
    open(os.path.join(st, "srt1.norun"), "w").write("1\n")  # strike 1 done
    res = agent.selfwatch_check_instance("srt1", st_dir=st,
                                         vol_base=tempfile.mkdtemp(),
                                         boot_recovery="progress")
    check(res is False, "R1 down instance still unhealthy")
    check(fd.starts() == [], "R2 active recovery -> NO docker start (deferred)")
    check("start deferred" in read_log(st), "R3 deferral log line")
    # completed recovery -> normal 2-strike start
    st2 = tempfile.mkdtemp()
    fd2 = FakeDocker(running=())
    agent.docker = fd2
    open(os.path.join(st2, "srt1.norun"), "w").write("1\n")
    res2 = agent.selfwatch_check_instance("srt1", st_dir=st2,
                                          vol_base=tempfile.mkdtemp(),
                                          boot_recovery="complete")
    check(fd2.starts() == [("start", "wd-srt1")],
          "R4 recovery complete -> normal 2-strike docker start",
          repr(fd2.starts()))
    # None (old direct callers) preserves the old behavior exactly
    st3 = tempfile.mkdtemp()
    fd3 = FakeDocker(running=())
    agent.docker = fd3
    open(os.path.join(st3, "srt1.norun"), "w").write("1\n")
    agent.selfwatch_check_instance("srt1", st_dir=st3,
                                   vol_base=tempfile.mkdtemp(),
                                   boot_recovery=None)
    check(fd3.starts() == [("start", "wd-srt1")],
          "R5 boot_recovery=None -> old behavior (no deferral)")
finally:
    agent.docker = orig_docker

print("== S. binder-control missing: mount path ==")
st, sd, up, b, fr = make_env(uptime_s=300.0, stock=0)
os.remove(os.path.join(b, "binder-control"))
fr2 = FakeRun(b, control_ok=False, mount_ok=True)
have, blocked = agent.sw_boot_binder_pool(st_dir=st, binderfs=b, run_fn=fr2,
                                          alert_dir=sd)
check(have == 64 and not blocked, "S1 mount ok -> pool restored",
      repr((have, blocked)))
check(any("modprobe binder_linux" in c for c in fr2.cmds),
      "S2 modprobe attempted")
fr3 = FakeRun(b, control_ok=False, mount_ok=False)
os.remove(os.path.join(b, "binder-control"))  # S1's fake mount recreated it
have3, blocked3 = agent.sw_boot_binder_pool(st_dir=st, binderfs=b, run_fn=fr3,
                                            alert_dir=sd)
check((have3, blocked3) == (0, False),
      "S3 mount fails -> (0, False), no raise", repr((have3, blocked3)))

print("== T. env sanitize ==")
os.environ["DF_BOOT_MAX_STARTS"] = "0"
os.environ["DF_BOOT_ATTEMPTS"] = "-5"
os.environ["DF_BOOT_WINDOW_S"] = "garbage"
os.environ["DF_BINDER_COUNT"] = ""
try:
    check(agent._sw_env_int_min("DF_BOOT_MAX_STARTS", 3, 1) == 3,
          "T1 zero max-starts -> default 3")
    check(agent._sw_env_int_min("DF_BOOT_ATTEMPTS", 3, 1) == 3,
          "T2 negative attempts -> default 3")
    check(agent._sw_env_int_min("DF_BOOT_WINDOW_S", 900, 1) == 900,
          "T3 garbage window -> default 900")
    check(agent._sw_env_int_min("DF_BINDER_COUNT", 64, 1) == 64,
          "T4 empty count -> default 64")
finally:
    for k in ("DF_BOOT_MAX_STARTS", "DF_BOOT_ATTEMPTS",
              "DF_BOOT_WINDOW_S", "DF_BINDER_COUNT"):
        os.environ.pop(k, None)

print("== U. invalid names never reach docker ==")
st, sd, up, b, fr = make_env(uptime_s=300.0)
fd = FakeDocker(running=())
r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                           binderfs=b, instances=["../x", "srt1;rm -rf /", "ok-1"],
                           docker_fn=fd, run_fn=fr)
check(r == "complete", "U1 only valid instance started", repr(r))
check(fd.starts() == [("start", "wd-ok-1")],
      "U2 invalid names filtered before docker", repr(fd.starts()))

print("== V. never-raises ==")


def boom_docker(*a, **k):
    raise RuntimeError("docker exploded")


def boom_run(*a, **k):
    raise RuntimeError("shell exploded")


st, sd, up, b, fr = make_env(uptime_s=300.0)
try:
    r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                               binderfs=b, instances=["srt1"],
                               docker_fn=boom_docker, run_fn=fr)
    check(r == "error", "V1 raising docker_fn -> error, no raise", repr(r))
except Exception as e:
    bad("V1 raising docker_fn must not raise", repr(e))
st, sd, up, b, fr = make_env(uptime_s=300.0, stock=0)
os.remove(os.path.join(b, "binder-control"))
try:
    r = agent.sw_boot_recovery(st_dir=st, sentinel_dir=sd, uptime_path=up,
                               binderfs=b, instances=["srt1"],
                               docker_fn=FakeDocker(running=()),
                               run_fn=boom_run)
    check(r in ("pool-paused", "incomplete", "error"),
          "V2 raising run_fn -> degraded status, no raise", repr(r))
except Exception as e:
    bad("V2 raising run_fn must not raise", repr(e))
try:
    check(agent.sw_boot_binder_pool(st_dir="/nonexistent/st", binderfs="/nonexistent/b",
                                    run_fn=boom_run) == (0, False),
          "V3 garbage paths -> (0, False), no raise")
except Exception as e:
    bad("V3 garbage paths must not raise", repr(e))

print("== W. pass_once wiring ==")
src = inspect.getsource(agent.selfwatch_pass_once)
i_boot = src.find("sw_boot_recovery(")
i_check = src.find("selfwatch_check_instance(")
check(i_boot != -1 and i_check != -1 and i_boot < i_check,
      "W1 section 0 runs before the per-instance checks")
check("boot_recovery=boot_status" in src,
      "W2 status passed into selfwatch_check_instance")
src2 = inspect.getsource(agent.selfwatch_check_instance)
check("boot recovery active, start deferred" in src2,
      "W3 deferral branch present in check_instance")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
