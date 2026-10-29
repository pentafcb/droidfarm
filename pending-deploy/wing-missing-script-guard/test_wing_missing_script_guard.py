#!/usr/bin/env python3
"""wing-missing-script-guard: agent 1.0.29 missing helper-script guard.

The 17th "main had it, wing didn't" parity item. The boot-complete work
(2026-10-20 ~14:05 UTC) added missing-helper-script guards to the main
watchdog's sections 3+4: a slow first boot on an image predating
boot-complete.sh finishes booting WITHOUT ever installing the helper
scripts, so restarting them fails silently (`waydroid shell sh <missing>`
fails inside Android while `docker exec -d` returns rc 0). The main
watchdog now logs one WARN/day naming the real cause and skips the restart
WITHOUT burning the circuit-breaker budget.

The wing agent's selfwatch had the same hole in check 2 (helper daemons)
and check 3 (stale screencap loop): it logged a lying "dead -> restart",
bumped the recovery count, and burned the 8/10min helper budget toward a
misleading DEGRADED. Agent 1.0.29 adds sw_warn_helper_missing() and guards
both checks identically to main.

Hermetic: fake docker + tmp state/volume trees, real agent import.
PASS=35 FAIL=0 expected.
"""
import importlib.util
import inspect
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")
WATCHDOG = os.path.join(REPO, "waydroid-image", "df-watchdog.sh")

PASS = FAIL = 0


def check(cond, name, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("wing_agent_under_test_29", AGENT)
app = load("manager_app_under_test_29", APP)

HELPERS = ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh")


class FakeDocker:
    """Stand-in for agent.docker: records calls, simulates container state."""

    def __init__(self):
        self.calls = []
        self.containers = {}
        self.helpers = {}      # (cname, script) -> alive bool
        self.session_alive = True

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[0] == "ps":
            return 0, "\n".join(self.containers.keys())
        if args[0] == "exec":
            cname = args[1]
            if "getprop" in args:
                return (0, "1\n") if self.session_alive else (1, "")
            if "pgrep" in args:
                script = args[-1]
                return (0, "4242\n") if self.helpers.get((cname, script)) else (1, "")
            if "pkill" in args:
                self.helpers[(cname, args[-1])] = False
                return 0, ""
            return 0, ""
        if args[0] == "start":
            self.containers[args[1]] = True
            return 0, ""
        return 0, ""

    def detaches(self):
        return [c for c in self.calls if c[0] == "exec" and "-d" in c]

    def pkills(self):
        return [c for c in self.calls if c[0] == "exec" and "pkill" in c]

    def read_log(self, st):
        p = os.path.join(st, "selfwatch.log")
        try:
            return open(p).read()
        except Exception:
            return ""


def make_env(helpers_alive=True, scripts_present=True, png_age=None):
    """Fresh tmp state dir + fake volume tree for instance srt1.

    The instance is modeled as booted (booted marker written) with a live
    Waydroid session, so checks 1c/1d are no-ops and the scenarios isolate
    checks 2 and 3.
    """
    st = tempfile.mkdtemp()
    vol = tempfile.mkdtemp()
    fake = FakeDocker()
    fake.containers["wd-srt1"] = True
    for s in HELPERS:
        fake.helpers[("wd-srt1", s)] = helpers_alive
    d = os.path.join(vol, "wd-srt1-data", "_data", "data", "media", "0", "jerusalem")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "booted"), "w").write("1\n")
    if scripts_present:
        for s in HELPERS:
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


def ev_files(st):
    return [f for f in os.listdir(st) if f.endswith(".ev")]


print("== A. VERSION + wiring ==")
check(agent.VERSION in ("1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"),
      "A1 VERSION is 1.0.29+ (1.0.30 adds update rollback)",
      repr(agent.VERSION))
check(agent.parse_agent_version("1.0.29") > agent.parse_agent_version("1.0.28"),
      "A2 parse_agent_version ordering: 1.0.29 > 1.0.28")
check(app._version_at_least("1.0.29", "1.0.2")
      and not app._version_at_least("1.0.1", "1.0.2"),
      "A3 manager >=1.0.2 gate accepts 1.0.29")
check(hasattr(agent, "sw_warn_helper_missing")
      and hasattr(agent, "sw_helper_script_path"),
      "A4 sw_warn_helper_missing + sw_helper_script_path exist")
src2 = inspect.getsource(agent.selfwatch_check_instance)
i_warn2 = src2.find("sw_warn_helper_missing(st_dir, name, script, vol_base)")
i_cb2 = src2.find('_sw_cb_guard(st_dir, name, key, 600, 8, script)')
check(i_warn2 != -1 and i_cb2 != -1 and i_warn2 < i_cb2,
      "A5 check 2 calls the missing-script guard BEFORE the circuit-breaker")
check('sw_warn_helper_missing(st_dir, name, "screencap-loop.sh", vol_base)' in src2,
      "A6 check 3 guards the stale-loop restart with the missing-script check")

print("== B. sw_helper_script_path ==")
check(agent.sw_helper_script_path("srt1", "screencap-loop.sh", "/vol")
      == "/vol/wd-srt1-data/_data/data/media/0/jerusalem/screencap-loop.sh",
      "B1 exact host path for (name, script, vol_base)")
check(agent.sw_helper_script_path("srt1", "input-daemon.sh")
      == "/var/lib/docker/volumes/wd-srt1-data/_data/data/media/0/jerusalem/input-daemon.sh",
      "B2 default vol_base")

print("== C. sw_warn_helper_missing units ==")
st, vol, fake = make_env()
d = os.path.join(vol, "wd-srt1-data", "_data", "data", "media", "0", "jerusalem")
check(agent.sw_warn_helper_missing(st, "srt1", "screencap-loop.sh", vol) is False,
      "C1 script present -> False, no marker")
check(not [f for f in os.listdir(st) if "helper-missing" in f],
      "C2 no marker file when script present")
os.remove(os.path.join(d, "input-daemon.sh"))
r = agent.sw_warn_helper_missing(st, "srt1", "input-daemon.sh", vol)
log = fake.read_log(st)
check(r is True, "C3 script missing -> True")
markers = [f for f in os.listdir(st) if "helper-missing-input_daemon_sh" in f]
check(len(markers) == 1 and markers[0].endswith(time.strftime("%F")),
      "C4 one dated marker file created", repr(markers))
check("WARN" in log and "input-daemon.sh" in log
      and "boot setup" in log and "never installed it" in log,
      "C5 WARN names the script and the real cause")
before = fake.read_log(st)
check(agent.sw_warn_helper_missing(st, "srt1", "input-daemon.sh", vol) is True
      and fake.read_log(st) == before,
      "C6 second call same day -> True, no duplicate WARN")
# yesterday's marker -> today's WARN fires again, stale marker cleaned
os.remove(os.path.join(st, markers[0]))
old = os.path.join(st, "srt1.helper-missing-input_daemon_sh.2000-01-01")
open(old, "w").close()
warns_before = fake.read_log(st).count("WARN")
check(agent.sw_warn_helper_missing(st, "srt1", "input-daemon.sh", vol) is True
      and fake.read_log(st).count("WARN") == warns_before + 1
      and not os.path.exists(old),
      "C7 stale marker -> new WARN today, old marker cleaned")
stuck = tempfile.mkdtemp()
stuck_file = os.path.join(stuck, "not-a-dir")
open(stuck_file, "w").write("x")
check(agent.sw_warn_helper_missing(stuck_file, "srt1", "x.sh", vol) is True,
      "C8 hostile st_dir (a file) -> True, never raises")

print("== D. check 2 integration: dead helpers ==")
st, vol, fake = make_env(helpers_alive=False, scripts_present=False)
use_fake(fake)
try:
    ok_green = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
log = fake.read_log(st)
check(ok_green is False, "D1 missing scripts -> instance unhealthy (blocks auto-clear)")
check(fake.detaches() == [], "D2 NO docker exec -d restart attempted for missing scripts")
check("dead -> restart" not in log, "D3 no lying 'dead -> restart' log line")
check(ev_files(st) == [], "D4 circuit-budget untouched (no .ev files)")
check(log.count("WARN") == 3 and all(s in log for s in HELPERS),
      "D5 one WARN per missing helper, each named")
check("recoveries.json" not in os.listdir(st)
      or open(os.path.join(st, "recoveries.json")).read().strip() in ("", '{"total": 0}'),
      "D6 recovery count NOT bumped for a skipped restart")

st, vol, fake = make_env(helpers_alive=False, scripts_present=True)
use_fake(fake)
try:
    agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
check(len(fake.detaches()) == 3, "D7 regression: present scripts still restarted (3x)")
check(len(ev_files(st)) == 3, "D8 regression: budget events recorded for real restarts")
check("dead -> restart" in fake.read_log(st),
      "D9 regression: 'dead -> restart' still logged when the script exists")

print("== E. check 3 integration: stale screencap ==")
st, vol, fake = make_env(helpers_alive=True, scripts_present=True, png_age=45)
os.remove(os.path.join(vol, "wd-srt1-data", "_data", "data", "media", "0",
                       "jerusalem", "screencap-loop.sh"))
use_fake(fake)
try:
    ok_green = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
log = fake.read_log(st)
check(ok_green is False, "E1 stale stream keeps instance unhealthy")
check(fake.pkills() == [] and fake.detaches() == [],
      "E2 NO pkill and NO loop restart when screencap-loop.sh is missing")
check("screencap-loop.sh" in log and "WARN" in log,
      "E3 distinct WARN for the missing loop script")
check(not os.path.exists(os.path.join(st, "srt1.staleloop.ev")),
      "E4 stale-loop circuit budget untouched")

st, vol, fake = make_env(helpers_alive=True, scripts_present=True, png_age=45)
use_fake(fake)
try:
    agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
check(len(fake.pkills()) == 1 and len(fake.detaches()) == 1,
      "E5 regression: present loop script still pkill'd + restarted on stale stream")

st, vol, fake = make_env(helpers_alive=True, scripts_present=False, png_age=5)
use_fake(fake)
try:
    ok_green = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
check(ok_green is True and "WARN" not in fake.read_log(st),
      "E6 fresh stream -> untouched, no WARN even with scripts missing")

print("== F. main-side parity source intact ==")
wd = open(WATCHDOG).read()
check("boot setup never installed it" in wd,
      "F1 main watchdog section 3/4 guard still present (parity source)")
check("cannot restart" in wd and "circuit-breaker budget" in wd,
      "F2 main guard still skips restart without burning budget")

print("== G. never-raises ==")
st, vol, fake = make_env()
use_fake(fake)
try:
    r = agent.selfwatch_check_instance("srt1", st_dir=st,
                                       vol_base="/nonexistent-vol-base")
    check(isinstance(r, bool), "G1 nonexistent vol_base -> bool, never raises")
    r = agent.selfwatch_check_instance("../x", st_dir=st, vol_base=vol)
    check(r is True and fake.detaches() == [],
          "G2 invalid name -> fullmatch-rejected early, no docker calls")
finally:
    restore()

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
