#!/usr/bin/env python3
"""Tests for wing agent 1.0.14 stuck-boot detection + auto-restart.

The main-node watchdog (df-watchdog.sh section 2) restarts a container
whose Android never finishes booting (up >15min, no `booted` marker), but
wing agents had no equivalent: on a wing node a stuck boot was invisible
(the drift 1b and session 1c checks skip never-booted instances), so only
the helper checks fired, burning the helper circuit budget until a
misleading "<script> restarted N times" DEGRADED, with the root cause
(dead boot) never repaired.

This suite covers:

  A. agent.VERSION is exactly 1.0.14 + manager >=1.0.2 gate compatibility
     + constants (900s / 2 strikes / 6-per-hour / 3600s window)
  B. _sw_parse_started_at units: Z with/without fraction, +HH:MM/-HHMM
     offsets (same-instant equality), garbage/empty/None/impossible ->
     None, never raises
  C. sw_check_stuck_boot skip vs stuck paths: invalid name and booted
     marker (strike file removed) -> "skip"; inspect rc!=0, garbage
     StartedAt, raising docker, and uptime <900s -> "stuck" (unhealthy
     pass, no strike, no restart — main-node parity: the marker is absent,
     but the restart is fail-open on ambiguous data)
  D. strike lifecycle: 901s -> "stuck" + strike "1"; second pass ->
     "restarted", strike file removed, exact log line, recoveries bumped;
     restart failure -> "stuck" + FAILED log + strikes kept (retry next
     minute)
  E. circuit budget: 7 restarts/hour -> 7th trips DEGRADED (reason on
     marker), no 7th docker restart, single DEGRADED log line, no spam
     while degraded
  F. selfwatch_check_instance wiring (check 1d): restarted -> early return
     (no helper pgreps on the restart pass); booted instance -> 1d skips
     (no inspect call) and the 1c session check owns it
  G. never raises (corrupt strike file, strike file is a dir, unwritable
     state dir)

All tests hermetic: fake docker, temp state/vol dirs, injected `now`.
"""
import importlib.util
import os
import sys
import tempfile
import time

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


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("wsb_agent", AGENT)
app = load("wsb_app", APP)

HELPERS = ["screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh"]


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class FakeDocker:
    """Stand-in for agent.docker: records calls, simulates node state."""

    def __init__(self):
        self.calls = []
        self.started_at = {}   # cname -> epoch float, raw string, or None
        self.inspect_rc = 0
        self.restart_rc = 0
        self.raise_all = False
        self.containers = {"wd-srt1": True}
        self.helpers = {}      # (cname, script) -> alive bool
        self.session_alive = True

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if self.raise_all:
            raise RuntimeError("docker down")
        if args[0] == "inspect":
            if self.inspect_rc != 0:
                return self.inspect_rc, "Error: No such object"
            cname = args[-1]
            v = self.started_at.get(cname)
            if v is None:
                return 1, "Error: No such object"
            if isinstance(v, str):
                return 0, v + "\n"
            return 0, iso(v) + "\n"
        if args[0] == "restart":
            if self.restart_rc != 0:
                return self.restart_rc, "restart failed"
            return 0, args[1] + "\n"
        if args[0] == "ps":
            return 0, "\n".join(self.containers.keys())
        if args[0] == "exec":
            cname = args[1]
            if "getprop" in args:
                return (0, "1\n") if self.session_alive else (1, "")
            if "pgrep" in args:
                script = args[-1]
                return (0, "4242\n") if self.helpers.get((cname, script)) else (1, "")
            return 0, ""
        if args[0] == "start":
            self.containers[args[1]] = True
            return 0, ""
        return 0, ""

    def restarts(self):
        return [c for c in self.calls if c[0] == "restart"]

    def inspects(self):
        return [c for c in self.calls if c[0] == "inspect"]

    def pgreps(self):
        return [c for c in self.calls if c[0] == "exec" and "pgrep" in c]


def make_env(booted=False, helpers_alive=True):
    st = tempfile.mkdtemp(prefix="wsb-st-")
    vol = tempfile.mkdtemp(prefix="wsb-vol-")
    fake = FakeDocker()
    for s in HELPERS:
        fake.helpers[("wd-srt1", s)] = helpers_alive
    if booted:
        d = os.path.join(vol, "wd-srt1-data", "_data", "data", "media", "0", "jerusalem")
        os.makedirs(d)
        open(os.path.join(d, "booted"), "w").write("1\n")
    return st, vol, fake


orig_docker = agent.docker


def use_fake(fake):
    agent.docker = fake


def restore():
    agent.docker = orig_docker


def log_text(d):
    p = os.path.join(d, "selfwatch.log")
    try:
        with open(p) as fh:
            return fh.read()
    except Exception:
        return ""


def recoveries(d):
    try:
        import json
        return json.load(open(os.path.join(d, "recoveries.json")))["total"]
    except Exception:
        return 0


def strike(d, name="srt1"):
    p = os.path.join(d, name + ".stuckboot")
    try:
        return open(p).read().strip()
    except Exception:
        return None


NOW = 1790000000.0  # fixed clock for the hermetic cases

print("== A. version + constants ==")
if agent.VERSION in ("1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"):
    ok("A1 VERSION is 1.0.14+")
else:
    bad("A1 VERSION is 1.0.14+", f"got {agent.VERSION!r}")
if app._version_at_least("1.0.14", "1.0.2") and not app._version_at_least("1.0.1", "1.0.2"):
    ok("A2 manager >=1.0.2 gate accepts 1.0.14")
else:
    bad("A2 manager >=1.0.2 gate accepts 1.0.14")
consts = (agent.SW_STUCK_BOOT_S, agent.SW_STUCK_BOOT_STRIKES,
          agent.SW_STUCK_BOOT_BUDGET_N, agent.SW_STUCK_BOOT_BUDGET_WINDOW_S)
if consts == (900, 2, 6, 3600):
    ok("A3 constants 900s / 2 strikes / 6-per-hour / 3600s (main section-2 parity)")
else:
    bad("A3 constants 900s / 2 strikes / 6-per-hour / 3600s", f"got {consts!r}")

print("== B. _sw_parse_started_at ==")
p = agent._sw_parse_started_at
if p("2026-10-16T15:30:00Z") == 1792140600.0 - 1792140600.0 + p("2026-10-16T15:30:00Z"):
    ok("B1 Zulu parses (self-consistent)")
else:
    bad("B1 Zulu parses")
# same instant expressed three ways
a = p("2026-10-16T15:30:00Z")
b = p("2026-10-16T17:30:00+02:00")
c = p("2026-10-16T11:00:00-0430")
if a is not None and b is not None and c is not None and abs(a - b) < 1 and abs(a - c) < 1:
    ok("B2 Z / +HH:MM / -HHMM name the same instant")
else:
    bad("B2 Z / +HH:MM / -HHMM name the same instant", f"{a} {b} {c}")
if p("2026-10-16T15:30:00.123456789Z") is not None and abs(
        p("2026-10-16T15:30:00.123456789Z") - p("2026-10-16T15:30:00Z") - 0.123456) < 0.001:
    ok("B3 fractional seconds -> microsecond precision")
else:
    bad("B3 fractional seconds")
for g in ("garbage", "", None, "2026-13-99T99:99:99Z", "2026-10-16 15:30:00",
          "2026-10-16T15:30:00+2", "0000-00-00T00:00:00Z"):
    try:
        r = p(g)
    except Exception as e:
        bad("B4 garbage never raises", f"{g!r} raised {e}")
        break
    if r is not None:
        bad("B4 garbage -> None", f"{g!r} -> {r}")
        break
else:
    ok("B4 garbage/empty/None/impossible -> None, never raises")

print("== C. sw_check_stuck_boot skip paths ==")
st, vol, fake = make_env(booted=False)
use_fake(fake)
fake.started_at["wd-srt1"] = NOW - 1000
if agent.sw_check_stuck_boot("../evil", st_dir=st, vol_base=vol, now=NOW) == "skip":
    ok("C1 invalid name -> skip")
else:
    bad("C1 invalid name -> skip")
if agent.sw_check_stuck_boot("", st_dir=st, vol_base=vol, now=NOW) == "skip":
    ok("C2 empty name -> skip")
else:
    bad("C2 empty name -> skip")
# booted -> skip + strike file removed
open(os.path.join(st, "srt1.stuckboot"), "w").write("2\n")
d = os.path.join(vol, "wd-srt1-data", "_data", "data", "media", "0", "jerusalem")
os.makedirs(d)
open(os.path.join(d, "booted"), "w").write("1\n")
if agent.sw_check_stuck_boot("srt1", st_dir=st, vol_base=vol, now=NOW) == "skip" \
        and strike(st) is None:
    ok("C3 booted marker -> skip + strike file removed")
else:
    bad("C3 booted marker -> skip + strike file removed")
os.remove(os.path.join(d, "booted"))
# inspect failing -> "stuck" (unhealthy pass, main parity: the marker is
# absent) but NO strike bump and NO restart — the restart is fail-open on
# ambiguous data. Same for garbage StartedAt and a raising docker.
fake.inspect_rc = 1
if agent.sw_check_stuck_boot("srt1", st_dir=st, vol_base=vol, now=NOW) == "stuck" \
        and strike(st) is None and not fake.restarts():
    ok("C4 inspect rc!=0 -> stuck, no strike, no restart")
else:
    bad("C4 inspect rc!=0 -> stuck, no strike, no restart")
fake.inspect_rc = 0
fake.started_at["wd-srt1"] = "not-a-timestamp"
if agent.sw_check_stuck_boot("srt1", st_dir=st, vol_base=vol, now=NOW) == "stuck" \
        and strike(st) is None and not fake.restarts():
    ok("C5 garbage StartedAt -> stuck, no strike, no restart")
else:
    bad("C5 garbage StartedAt -> stuck, no strike, no restart")
fake.started_at["wd-srt1"] = NOW - 1000
fake.raise_all = True
if agent.sw_check_stuck_boot("srt1", st_dir=st, vol_base=vol, now=NOW) == "stuck":
    ok("C6 docker raising -> stuck, never raises")
else:
    bad("C6 docker raising -> stuck")
fake.raise_all = False
# uptime under the line -> stuck, no strike file, no docker restart
fake.started_at["wd-srt1"] = NOW - 899
if agent.sw_check_stuck_boot("srt1", st_dir=st, vol_base=vol, now=NOW) == "stuck" \
        and strike(st) is None and not fake.restarts():
    ok("C7 uptime 899s -> stuck, no strike, no restart")
else:
    bad("C7 uptime 899s -> stuck, no strike, no restart")

print("== D. strike lifecycle ==")
fake.started_at["wd-srt1"] = NOW - 901
r = agent.sw_check_stuck_boot("srt1", st_dir=st, vol_base=vol, now=NOW)
if r == "stuck" and strike(st) == "1" and not fake.restarts():
    ok("D1 first stuck pass -> strike 1, no restart")
else:
    bad("D1 first stuck pass", f"got {r!r} strike={strike(st)!r}")
r = agent.sw_check_stuck_boot("srt1", st_dir=st, vol_base=vol, now=NOW)
log = log_text(st)
if r == "restarted" and strike(st) is None and len(fake.restarts()) == 1 \
        and fake.restarts()[0][1] == "wd-srt1" \
        and "srt1 stuck booting (no marker after 15m) -> docker restart (1/6 this hour)" in log \
        and recoveries(st) == 1:
    ok("D2 second stuck pass -> docker restart, strike reset, exact log, recovery bumped")
else:
    bad("D2 second stuck pass", f"got {r!r}")
# restart failure -> stuck + FAILED log + strikes kept for next minute
fake.restart_rc = 1
open(os.path.join(st, "srt1.stuckboot"), "w").write("2\n")
r = agent.sw_check_stuck_boot("srt1", st_dir=st, vol_base=vol, now=NOW)
if r == "stuck" and strike(st) == "3" and "srt1 stuck-boot restart FAILED" in log_text(st):
    ok("D3 restart failure -> stuck + FAILED log + strikes kept")
else:
    bad("D3 restart failure", f"got {r!r} strike={strike(st)!r}")
fake.restart_rc = 0

print("== E. circuit budget ==")
st2, vol2, fake2 = make_env(booted=False)
use_fake(fake2)
fake2.started_at["wd-srt1"] = NOW - 901
for i in range(7):
    open(os.path.join(st2, "srt1.stuckboot"), "w").write("9\n")
    r = agent.sw_check_stuck_boot("srt1", st_dir=st2, vol_base=vol2, now=NOW)
log2 = log_text(st2)
marker = os.path.join(st2, "srt1.degraded")
reason = open(marker).readline().strip() if os.path.exists(marker) else ""
if len(fake2.restarts()) == 6 and r == "stuck" and os.path.exists(marker) \
        and "container restarted" in reason and "auto-recovery paused" in reason \
        and log2.count("DEGRADED") == 1:
    ok("E1 7th restart/hour -> DEGRADED (single line, reason on marker, no 7th restart)")
else:
    bad("E1 budget trip", f"restarts={len(fake2.restarts())} r={r!r} reason={reason!r}")
n_restarts = len(fake2.restarts())
n_logs = len(log2)
open(os.path.join(st2, "srt1.stuckboot"), "w").write("9\n")
r = agent.sw_check_stuck_boot("srt1", st_dir=st2, vol_base=vol2, now=NOW)
if r == "stuck" and len(fake2.restarts()) == n_restarts \
        and len(log_text(st2)) == n_logs:
    ok("E2 while degraded -> silent, no restart, no log spam")
else:
    bad("E2 while degraded")

print("== F. selfwatch_check_instance wiring (check 1d) ==")
st3, vol3, fake3 = make_env(booted=False, helpers_alive=False)
use_fake(fake3)
fake3.started_at["wd-srt1"] = time.time() - 1000
# pass 1: strike 1 -> helpers still checked (main parity: no early return pre-restart)
ret1 = agent.selfwatch_check_instance("srt1", st_dir=st3, vol_base=vol3)
pgreps1 = len(fake3.pgreps())
# pass 2: restart fires -> early return, no helper pgreps on this pass
fake3.calls.clear()
ret2 = agent.selfwatch_check_instance("srt1", st_dir=st3, vol_base=vol3)
pgreps2 = len(fake3.pgreps())
if ret1 is False and pgreps1 > 0 and ret2 is False and pgreps2 == 0 \
        and len(fake3.restarts()) == 1 \
        and "stuck booting (no marker after 15m) -> docker restart" in log_text(st3):
    ok("F1 restarted -> early return (helpers untouched on the restart pass)")
else:
    bad("F1 early return", f"ret1={ret1} pgreps1={pgreps1} ret2={ret2} pgreps2={pgreps2}")
# booted instance: 1d skips (no inspect), the 1c session check owns it
st4, vol4, fake4 = make_env(booted=True, helpers_alive=False)
use_fake(fake4)
fake4.session_alive = False
fake4.calls.clear()
ret4 = agent.selfwatch_check_instance("srt1", st_dir=st4, vol_base=vol4)
if ret4 is False and not fake4.inspects() \
        and any("session" in c and "start" in c for c in fake4.calls) \
        and not fake4.restarts():
    ok("F2 booted -> no inspect from 1d; 1c session-death path owns it")
else:
    bad("F2 booted instance", f"inspects={fake4.inspects()}")
# inspect call shape
fake5 = FakeDocker()
st5, vol5, _ = make_env(booted=False)
use_fake(fake5)
fake5.started_at["wd-srt1"] = NOW - 100
agent.sw_check_stuck_boot("srt1", st_dir=st5, vol_base=vol5, now=NOW)
if fake5.inspects() and fake5.inspects()[0] == ("inspect", "-f", "{{.State.StartedAt}}", "wd-srt1"):
    ok("F3 inspect args exact: -f {{.State.StartedAt}} wd-<name>")
else:
    bad("F3 inspect args", f"{fake5.inspects()}")

print("== G. never raises ==")
st6, vol6, fake6 = make_env(booted=False)
use_fake(fake6)
fake6.started_at["wd-srt1"] = NOW - 1000
open(os.path.join(st6, "srt1.stuckboot"), "w").write("garbage\n")
try:
    r = agent.sw_check_stuck_boot("srt1", st_dir=st6, vol_base=vol6, now=NOW)
    ok("G1 corrupt strike file -> treated as 0, never raises") if r == "stuck" else bad("G1 corrupt strike file", f"got {r!r}")
except Exception as e:
    bad("G1 corrupt strike file raised", repr(e))
os.remove(os.path.join(st6, "srt1.stuckboot"))
os.makedirs(os.path.join(st6, "srt1.stuckboot"))
try:
    r = agent.sw_check_stuck_boot("srt1", st_dir=st6, vol_base=vol6, now=NOW)
    ok("G2 strike path is a dir -> never raises") if r in ("stuck", "skip") else bad("G2 strike dir", f"got {r!r}")
except Exception as e:
    bad("G2 strike dir raised", repr(e))
try:
    r = agent.sw_check_stuck_boot("srt1", st_dir="/tmp/definitely-not-here-wsb/state",
                                  vol_base="/tmp/definitely-not-here-wsb/vol", now=NOW)
    ok("G3 missing state/vol dirs -> stuck, never raises") if r == "stuck" else bad("G3 missing dirs", f"got {r!r}")
except Exception as e:
    bad("G3 missing dirs raised", repr(e))
# default now= (no arg) against real clock
st7, vol7, fake7 = make_env(booted=False)
use_fake(fake7)
fake7.started_at["wd-srt1"] = time.time() - 1000
try:
    r = agent.sw_check_stuck_boot("srt1", st_dir=st7, vol_base=vol7)
    ok("G4 now defaults to time.time()") if r == "stuck" and strike(st7) == "1" else bad("G4 default now", f"got {r!r}")
except Exception as e:
    bad("G4 default now raised", repr(e))

restore()
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
