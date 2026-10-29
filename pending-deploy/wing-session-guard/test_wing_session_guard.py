#!/usr/bin/env python3
"""Tests for wing agent 1.0.11 waydroid session-death guard (v9 parity).

Remote nodes had no equivalent of the main-node watchdog's v9 session
check: a dead Waydroid session inside a running container was invisible to
the agent's selfwatch (container up, booted marker persisted, helpers run
through `waydroid shell` so their restarts failed silently while burning
the helper circuit budget). This suite covers:

  A. agent.VERSION bump 1.0.10 -> 1.0.11 + manager gate compat
  B. sw_booted_once marker paths / invalid / absent
  C. sw_session_probe alive/dead/failed/raising docker
  D. sw_check_session skip paths (invalid name, never booted)
  E. alive -> no action, stale grace ts dropped
  F. dead -> detached session start, grace ts, log, recovery bump
  G. selfwatch_check_instance early return: helpers NOT restarted on death
  H. grace: no second start within 180s, "waiting" log
  I. grace expiry -> new start issued
  J. failed start -> FAILED log, no grace ts (retry next minute)
  K. budget: >4 starts/hour -> DEGRADED marker (reason first line), paused
  L. while degraded: no docker calls, no log spam
  M. auto-clear: alive + all green -> markers + grace ts removed, recovered
  N. corrupt grace ts / raising docker -> never raises
  O. wiring contract: 1c placement + early return in selfwatch_check_instance
  P. degraded marker flows through selfwatch_state() (dashboard visibility)
"""
import importlib.util
import inspect
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


agent = load("sg_agent", AGENT)
app = load("sg_app", APP)

HELPERS = ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh")


class FakeDocker:
    """Stand-in for agent.docker: records calls, simulates node state."""

    def __init__(self):
        self.calls = []
        self.containers = {}        # cname -> True (up)
        self.helpers = {}           # (cname, script) -> alive bool
        self.session_alive = True   # waydroid session probe result
        self.session_raise = False  # probe raises instead of answering
        self.start_rc = 0           # rc of detached `waydroid session start`
        self.ps_rc = 0              # rc of `docker ps`
        self.raise_all = False      # every docker call raises

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if self.raise_all:
            raise RuntimeError("docker down")
        if args[0] == "ps":
            if self.ps_rc != 0:
                return self.ps_rc, "Cannot connect to the Docker daemon"
            return 0, "\n".join(self.containers.keys())
        if args[0] == "exec":
            if "-d" in args:
                # detached session start (or helper restart)
                if "session" in args and "start" in args:
                    return self.start_rc, ""
                return 0, ""
            cname = args[1]
            if "getprop" in args:
                if self.session_raise:
                    raise RuntimeError("exec failed")
                return (0, "1\n") if self.session_alive else (1, "")
            if "pgrep" in args:
                script = args[-1]
                return (0, "4242\n") if self.helpers.get((cname, script)) else (1, "")
            return 0, ""
        if args[0] == "start":
            if self.start_rc != 0:
                return self.start_rc, "start failed"
            self.containers[args[1]] = True
            return 0, ""
        return 0, ""

    def session_starts(self):
        return [c for c in self.calls
                if c[0] == "exec" and "-d" in c and "session" in c and "start" in c]

    def helper_restarts(self):
        return [c for c in self.calls
                if c[0] == "exec" and "-d" in c and "waydroid" in c and "shell" in c]

    def read_log(self, st):
        try:
            return open(os.path.join(st, "selfwatch.log")).read()
        except Exception:
            return ""


def make_env(booted=True, helpers_alive=True):
    st = tempfile.mkdtemp()
    vol = tempfile.mkdtemp()
    fake = FakeDocker()
    fake.containers["wd-srt1"] = True
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


def recoveries(st):
    try:
        import json
        return json.load(open(os.path.join(st, "recoveries.json")))["total"]
    except Exception:
        return 0


print("== A. version bump + gate compat ==")
# Widened for 1.0.12 (loop-device guard); the exact-bump assertion lives in
# the wing-loop-devices suite now.
if agent.VERSION in ("1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"):
    ok("A1 VERSION is 1.0.11+")
else:
    bad("A1 VERSION is 1.0.11+", f"got {agent.VERSION!r}")
if app._version_at_least("1.0.12", "1.0.2") and not app._version_at_least("1.0.1", "1.0.2"):
    ok("A2 manager >=1.0.2 gate accepts 1.0.12")
else:
    bad("A2 manager >=1.0.2 gate accepts 1.0.12")
if (agent.SW_SESSION_GRACE_S, agent.SW_SESSION_BUDGET_N, agent.SW_SESSION_BUDGET_WINDOW_S) == (180, 4, 3600):
    ok("A3 constants 180/4/3600 (v9 parity)")
else:
    bad("A3 constants 180/4/3600 (v9 parity)")

print("== B. sw_booted_once ==")
st, vol, fake = make_env(booted=True)
use_fake(fake)
if agent.sw_booted_once("srt1", vol):
    ok("B1 marker data/media/0/jerusalem/booted -> True")
else:
    bad("B1 marker data/media/0/jerusalem/booted -> True")
st2, vol2, fake2 = make_env(booted=False)
base2 = os.path.join(vol2, "wd-srt1-data", "_data")
os.makedirs(os.path.join(base2, "data"), exist_ok=True)
open(os.path.join(base2, "data", "booted"), "w").write("1\n")
if agent.sw_booted_once("srt1", vol2):
    ok("B2 legacy data/booted path -> True")
else:
    bad("B2 legacy data/booted path -> True")
if not agent.sw_booted_once("srt1", "/tmp/definitely-not-here-xyz"):
    ok("B3 no marker -> False")
else:
    bad("B3 no marker -> False")
if not agent.sw_booted_once("../x", vol) and not agent.sw_booted_once("", vol):
    ok("B4 invalid names -> False")
else:
    bad("B4 invalid names -> False")

print("== C. sw_session_probe ==")
fake.session_alive = True
if agent.sw_session_probe("wd-srt1") is True:
    ok("C1 boot_completed=1 -> True")
else:
    bad("C1 boot_completed=1 -> True")
fake.session_alive = False
if agent.sw_session_probe("wd-srt1") is False:
    ok("C2 dead session -> False")
else:
    bad("C2 dead session -> False")
fake.session_alive = True
fake.session_raise = True
if agent.sw_session_probe("wd-srt1") is False:
    ok("C3 raising docker -> False, never raises")
else:
    bad("C3 raising docker -> False, never raises")
fake.session_raise = False
fake.raise_all = True
if agent.sw_session_probe("wd-srt1") is False:
    ok("C4 docker down -> False, never raises")
else:
    bad("C4 docker down -> False, never raises")
fake.raise_all = False

print("== D. sw_check_session skip paths ==")
if agent.sw_check_session("../evil", st_dir=st, vol_base=vol) == "skip":
    ok("D1 invalid name -> skip")
else:
    bad("D1 invalid name -> skip")
st_nb, vol_nb, fake_nb = make_env(booted=False)
agent.docker = fake_nb
if agent.sw_check_session("srt1", st_dir=st_nb, vol_base=vol_nb) == "skip":
    ok("D2 never booted -> skip (first boot not probed)")
else:
    bad("D2 never booted -> skip (first boot not probed)")
if fake_nb.session_starts() == []:
    ok("D3 skip performs no docker session calls")
else:
    bad("D3 skip performs no docker session calls", fake_nb.session_starts())

print("== E. alive -> no action, grace ts dropped ==")
use_fake(fake)
fake.session_alive = True
open(os.path.join(st, "srt1.sessionstart.ts"), "w").write("12345\n")
r = agent.sw_check_session("srt1", st_dir=st, vol_base=vol)
if r == "alive":
    ok("E1 alive session -> 'alive'")
else:
    bad("E1 alive session -> 'alive'", repr(r))
if not os.path.exists(os.path.join(st, "srt1.sessionstart.ts")):
    ok("E2 stale grace ts removed on alive probe")
else:
    bad("E2 stale grace ts removed on alive probe")
if fake.session_starts() == [] and recoveries(st) == 0:
    ok("E3 no start issued, no recovery counted")
else:
    bad("E3 no start issued, no recovery counted")

print("== F. dead -> detached session start ==")
fake.session_alive = False
r = agent.sw_check_session("srt1", st_dir=st, vol_base=vol)
starts = fake.session_starts()
if r == "started":
    ok("F1 dead session -> 'started'")
else:
    bad("F1 dead session -> 'started'", repr(r))
if len(starts) == 1 and starts[0][2] == "wd-srt1" and "waydroid" in starts[0] and "start" in starts[0]:
    ok("F2 detached `docker exec -d wd-srt1 waydroid session start`")
else:
    bad("F2 detached session start", starts)
if os.path.exists(os.path.join(st, "srt1.sessionstart.ts")):
    ok("F3 grace ts written")
else:
    bad("F3 grace ts written")
if "waydroid session dead -> session start (1/4 this hour)" in fake.read_log(st):
    ok("F4 log line names the recovery + budget")
else:
    bad("F4 log line names the recovery + budget", fake.read_log(st)[-200:])
if recoveries(st) == 1:
    ok("F5 recovery counted")
else:
    bad("F5 recovery counted", recoveries(st))

print("== G. selfwatch_check_instance early return on death ==")
# helper dead too: without the early return the helper check would restart it
fake.helpers[("wd-srt1", "input-daemon.sh")] = False
n_calls_before = len(fake.calls)
res = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
new_calls = fake.calls[n_calls_before:]
# within grace now -> "grace", still early return
if res is False:
    ok("G1 check returns False (unhealthy) on dead session")
else:
    bad("G1 check returns False (unhealthy) on dead session", repr(res))
if [c for c in new_calls if c[0] == "exec" and "-d" in c and "waydroid" in c and "shell" in c] == []:
    ok("G2 no helper restart attempted (early return, budget not burned)")
else:
    bad("G2 no helper restart attempted", new_calls)

print("== H. grace: no second start within 180s ==")
r = agent.sw_check_session("srt1", st_dir=st, vol_base=vol)
if r == "grace":
    ok("H1 second pass in grace -> 'grace'")
else:
    bad("H1 second pass in grace -> 'grace'", repr(r))
if len(fake.session_starts()) == 1:
    ok("H2 no additional session start during grace")
else:
    bad("H2 no additional session start during grace", len(fake.session_starts()))
if "still down" in fake.read_log(st) and "180s grace" in fake.read_log(st):
    ok("H3 'still down ... 180s grace - waiting' logged")
else:
    bad("H3 grace waiting log", fake.read_log(st)[-200:])

print("== I. grace expiry -> new start ==")
tsf = os.path.join(st, "srt1.sessionstart.ts")
open(tsf, "w").write(str(int(time.time()) - 200) + "\n")
r = agent.sw_check_session("srt1", st_dir=st, vol_base=vol)
if r == "started" and len(fake.session_starts()) == 2:
    ok("I1 expired grace -> new session start issued")
else:
    bad("I1 expired grace -> new session start issued", (r, len(fake.session_starts())))

print("== J. failed start -> FAILED log, no grace ts ==")
fake.start_rc = 1
os.remove(tsf)
r = agent.sw_check_session("srt1", st_dir=st, vol_base=vol)
if r == "failed":
    ok("J1 failed start -> 'failed'")
else:
    bad("J1 failed start -> 'failed'", repr(r))
if "waydroid session start FAILED" in fake.read_log(st):
    ok("J2 FAILED logged")
else:
    bad("J2 FAILED logged")
if not os.path.exists(tsf):
    ok("J3 no grace ts on failure (retry next minute)")
else:
    bad("J3 no grace ts on failure")
fake.start_rc = 0

print("== K. budget: >4 starts/hour -> DEGRADED ==")
st_k, vol_k, fake_k = make_env(booted=True)
use_fake(fake_k)
fake_k.session_alive = False
# burn 4 budgeted starts (spacing the grace ts to force expiry each pass)
results = []
for i in range(4):
    tsf_k = os.path.join(st_k, "srt1.sessionstart.ts")
    if os.path.exists(tsf_k):
        os.remove(tsf_k)
    results.append(agent.sw_check_session("srt1", st_dir=st_k, vol_base=vol_k))
if results == ["started"] * 4 and len(fake_k.session_starts()) == 4:
    ok("K1 four starts allowed within the hour")
else:
    bad("K1 four starts allowed within the hour", (results, len(fake_k.session_starts())))
tsf_k = os.path.join(st_k, "srt1.sessionstart.ts")
if os.path.exists(tsf_k):
    os.remove(tsf_k)
r = agent.sw_check_session("srt1", st_dir=st_k, vol_base=vol_k)
if r == "paused":
    ok("K2 5th start in the hour -> 'paused' (budget exhausted)")
else:
    bad("K2 5th start in the hour -> 'paused'", repr(r))
if len(fake_k.session_starts()) == 4:
    ok("K3 no 5th session start issued")
else:
    bad("K3 no 5th session start issued", len(fake_k.session_starts()))
mk = os.path.join(st_k, "srt1.degraded")
reason = open(mk).readline().strip() if os.path.exists(mk) else ""
if os.path.exists(mk) and "waydroid session" in reason and "auto-recovery paused" in reason:
    ok("K4 .degraded marker written, reason on first line")
else:
    bad("K4 .degraded marker written, reason on first line", repr(reason))
if fake_k.read_log(st_k).count("DEGRADED") == 1:
    ok("K5 single DEGRADED log line (no spam)")
else:
    bad("K5 single DEGRADED log line", fake_k.read_log(st_k).count("DEGRADED"))

print("== L. while degraded: silent, no new starts ==")
n_starts_before = len(fake_k.session_starts())
r = agent.sw_check_session("srt1", st_dir=st_k, vol_base=vol_k)
# NOTE: the getprop probe still runs once per pass while degraded — it is
# what detects recovery (v9 parity); only the *start* is gated.
if r == "paused" and len(fake_k.session_starts()) == n_starts_before:
    ok("L1 degraded -> 'paused', no new session start (guard short-circuits)")
else:
    bad("L1 degraded -> 'paused', no new session start", (r, len(fake_k.session_starts()) - n_starts_before))
if fake_k.read_log(st_k).count("DEGRADED") == 1:
    ok("L2 still exactly one DEGRADED line")
else:
    bad("L2 still exactly one DEGRADED line")

print("== M. auto-clear on recovery ==")
fake_k.session_alive = True  # session back
# The M pass must model a fully-green instance: give it a healthy stream
# (helper scripts + fresh live.png), otherwise the missing-frame guard
# (agent 1.0.42) correctly flags the pass unhealthy for the missing frame
# and the auto-clear under test never runs — which is the stream check's
# business, not the session guard's.
_m_base = os.path.join(vol_k, "wd-srt1-data", "_data", "data", "media",
                       "0", "jerusalem")
for _s in ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh"):
    open(os.path.join(_m_base, _s), "w").write("#!/bin/sh\n")
open(os.path.join(_m_base, "live.png"), "w").write("x")
res = agent.selfwatch_check_instance("srt1", st_dir=st_k, vol_base=vol_k)
leftovers = [f for f in os.listdir(st_k)
             if f.startswith("srt1.") and (f.endswith(".ev") or f.endswith(".degraded"))]
if res is True and leftovers == [] and not os.path.exists(os.path.join(st_k, "srt1.sessionstart.ts")):
    ok("M1 all green -> True, .degraded + .ev + grace ts removed")
else:
    bad("M1 all green -> True, markers removed", (res, leftovers))
if "recovered from DEGRADED" in fake_k.read_log(st_k):
    ok("M2 'recovered from DEGRADED' logged")
else:
    bad("M2 'recovered from DEGRADED' logged")

print("== N. hostile inputs never raise ==")
st_n, vol_n, fake_n = make_env(booted=True)
use_fake(fake_n)
fake_n.session_alive = False
open(os.path.join(st_n, "srt1.sessionstart.ts"), "w").write("garbage-not-a-number\n")
try:
    r = agent.sw_check_session("srt1", st_dir=st_n, vol_base=vol_n)
    ok("N1 corrupt grace ts -> proceeds, never raises (%r)" % r)
except Exception as e:
    bad("N1 corrupt grace ts -> never raises", repr(e))
fake_n.raise_all = True
try:
    r = agent.sw_check_session("srt1", st_dir=st_n, vol_base=vol_n)
    ok("N2 raising docker -> sw_check_session never raises (%r)" % r)
except Exception as e:
    bad("N2 raising docker -> sw_check_session never raises", repr(e))
fake_n.raise_all = False
# production-faithful "docker down": run() never raises, it returns rc != 0
fake_n.ps_rc = 1
fake_n.start_rc = 1
try:
    res = agent.selfwatch_check_instance("srt1", st_dir=st_n, vol_base=vol_n)
    ok("N3 docker-down (rc=1) -> instance check no raise, returns %r" % res)
except Exception as e:
    bad("N3 docker-down (rc=1) -> instance check no raise", repr(e))

print("== O. wiring contract ==")
src = inspect.getsource(agent.selfwatch_check_instance)
if "sw_check_session(name, st_dir=st_dir, vol_base=vol_base)" in src:
    ok("O1 1c calls sw_check_session with st_dir + vol_base")
else:
    bad("O1 1c calls sw_check_session with st_dir + vol_base")
seg = src.split("1c. waydroid session alive?")[1].split("# 2. helper daemons")[0]
if 'return False' in seg and '"alive"' in seg and '"skip"' in seg:
    ok("O2 early return on death (not alive/skip)")
else:
    bad("O2 early return on death (not alive/skip)")
if "sessionstart.ts" in src:
    ok("O3 step-4 cleanup drops the grace ts")
else:
    bad("O3 step-4 cleanup drops the grace ts")
if src.index("sw_check_session") > src.index("check_roblox_drift") and \
        src.index("sw_check_session") < src.index("_SELF_WATCH_HELPERS"):
    ok("O4 1c sits between drift check and helper checks")
else:
    bad("O4 1c sits between drift check and helper checks")

print("== P. degraded marker reaches the dashboard path ==")
st_p, vol_p, fake_p = make_env(booted=True)
use_fake(fake_p)
fake_p.session_alive = False
for _ in range(5):
    tsf_p = os.path.join(st_p, "srt1.sessionstart.ts")
    if os.path.exists(tsf_p):
        os.remove(tsf_p)
    agent.sw_check_session("srt1", st_dir=st_p, vol_base=vol_p)
state = agent.selfwatch_state(st_dir=st_p)
d = state.get("degraded", {})
if "srt1" in d and "waydroid session" in d["srt1"] and len(d["srt1"]) <= 160:
    ok("P1 selfwatch_state degraded map carries the session reason (<=160)")
else:
    bad("P1 selfwatch_state degraded map carries the session reason", d)
# build_heartbeat_payload() wires the module-global selfwatch_state(), so the
# P1-proven degraded map reaches the heartbeat -> the dashboard Nodes badge.
# (The payload reads SELF_WATCH_DIR, not injectable here; the remote-selfwatch
# suite covers the live payload shape.)
if "selfwatch_state()" in inspect.getsource(agent.build_heartbeat_payload):
    ok("P2 heartbeat payload wired to selfwatch_state() (dashboard Nodes badge, no manager change)")
else:
    bad("P2 heartbeat payload wired to selfwatch_state()")

restore()
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
