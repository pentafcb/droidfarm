#!/usr/bin/env python3
"""Tests for wing agent 1.0.15 keepalive crash-loop surfacing (main-node v8/3b parity).

REAL GAP: the main node's df-watchdog mirrors the in-container
roblox-keepalive.sh v2's keepalive.degraded marker into the instance .degraded
marker (check 3b), so the dashboard "needs attention" badge shows a
crash-looping Roblox. Wing agents (1.0.14) never did: on a wing node the
keepalive silently backed off to 5-min attempts while the node badge stayed
green. This suite covers:

  A. agent.VERSION is 1.0.15+ (exact bump now owned by wing-binderfs-selfheal)
     + manager >=1.0.2 gate compatibility
  B. sw_keepalive_marker_path: valid -> host-side volume path; invalid ->
     None; never raises
  C. no marker file -> "ok", no st_dir writes, no log spam
  D. marker present -> "degraded", <name>.degraded written with the
     "Roblox keepalive: <reason>" first line, exactly one DEGRADED log line
  E. no spam: second pass with marker still present adds no log line
  F. pre-existing <name>.degraded (other cause) is never clobbered
  G. empty marker file -> default reason; long reason truncated to 160
  H. selfwatch_check_instance wiring: marker present -> unhealthy (False);
     absent -> healthy (True) on a fully-green fake node
  I. recovery: keepalive clears its marker -> next pass auto-clears the
     mirror + logs "recovered"
  J. invalid names -> "skip"; unreadable volume/st_dir -> "ok"/tolerated;
     never raises
  K. wiring contract: 3b block placement, healthy=False on degraded,
     never touches cb_guard
  L. mirror reason flows through selfwatch_state() degraded map (dashboard
     Nodes badge path, no manager/dashboard change)
  M. SW_KEEPALIVE_DEGRADED constant == "keepalive.degraded"
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


agent = load("km_agent", AGENT)
app = load("km_app", APP)

HELPERS = ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh")
REASON = "Roblox launch failing (13 attempts in the last hour) - auto-launch backed off, retrying every 5 min"


class FakeDocker:
    """Stand-in for agent.docker: records calls, simulates node state."""

    def __init__(self):
        self.calls = []
        self.containers = {}
        self.helpers = {}
        self.session_alive = True
        self.ps_rc = 0

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[0] == "ps":
            if self.ps_rc != 0:
                return self.ps_rc, "Cannot connect to the Docker daemon"
            return 0, "\n".join(self.containers.keys())
        if args[0] == "exec":
            if "-d" in args:
                return 0, ""
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


def make_env(booted=True, helpers_alive=True, keepalive_marker=None):
    st = tempfile.mkdtemp()
    vol = tempfile.mkdtemp()
    fake = FakeDocker()
    fake.containers["wd-srt1"] = True
    for s in HELPERS:
        fake.helpers[("wd-srt1", s)] = helpers_alive
    jd = os.path.join(vol, "wd-srt1-data", "_data", "data", "media", "0", "jerusalem")
    os.makedirs(jd)
    if booted:
        open(os.path.join(jd, "booted"), "w").write("1\n")
    # fresh live.png so the stale-stream check stays green
    open(os.path.join(jd, "live.png"), "w").write("png")
    if keepalive_marker is not None:
        open(os.path.join(jd, "keepalive.degraded"), "w").write(keepalive_marker)
    return st, vol, fake


orig_docker = agent.docker


def install(fake):
    agent.docker = fake


def restore():
    agent.docker = orig_docker


def read_log(st):
    try:
        return open(os.path.join(st, "selfwatch.log")).read()
    except Exception:
        return ""


NOW = 1790000000.0

print("== A. version + gate ==")
if agent.VERSION in ("1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"):
    ok("A1 VERSION is 1.0.15+")
else:
    bad("A1 VERSION is 1.0.15+", f"got {agent.VERSION!r}")
if app._version_at_least("1.0.15", "1.0.2") and not app._version_at_least("1.0.1", "1.0.2"):
    ok("A2 manager >=1.0.2 gate accepts 1.0.15")
else:
    bad("A2 manager >=1.0.2 gate accepts 1.0.15")

print("== B. marker path ==")
st, vol, fake = make_env()
p = agent.sw_keepalive_marker_path("srt1", vol)
want = os.path.join(vol, "wd-srt1-data", "_data", "data", "media", "0",
                    "jerusalem", "keepalive.degraded")
if p == want:
    ok("B1 valid name -> host-side data-volume path")
else:
    bad("B1 valid name -> host-side data-volume path", f"got {p!r}")
for n in ("../x", "", None, "a" * 33, "srt 1", "srt;1"):
    try:
        r = agent.sw_keepalive_marker_path(n, vol)
    except Exception as e:
        bad("B2 invalid name never raises", f"{n!r} raised {e!r}")
        continue
    if r is None:
        ok(f"B2 invalid name {n!r} -> None")
    else:
        bad("B2 invalid name -> None", f"{n!r} -> {r!r}")
restore()

print("== C. no marker -> ok, silent ==")
st, vol, fake = make_env()
install(fake)
r = agent.sw_check_keepalive_degraded("srt1", st_dir=st, vol_base=vol)
restore()
if r == "ok":
    ok("C1 no marker file -> 'ok'")
else:
    bad("C1 no marker file -> 'ok'", f"got {r!r}")
left = [f for f in os.listdir(st) if not f == "selfwatch.log"]
if not left and read_log(st) == "":
    ok("C2 no st_dir writes, no log spam when healthy")
else:
    bad("C2 no st_dir writes, no log spam when healthy", f"left={left}")

print("== D. marker present -> degraded mirror ==")
st, vol, fake = make_env(keepalive_marker=REASON + "\n")
install(fake)
r = agent.sw_check_keepalive_degraded("srt1", st_dir=st, vol_base=vol)
restore()
mp = os.path.join(st, "srt1.degraded")
if r == "degraded":
    ok("D1 marker present -> 'degraded'")
else:
    bad("D1 marker present -> 'degraded'", f"got {r!r}")
try:
    first = open(mp).readline().strip()
except Exception as e:
    first = None
    bad("D2 mirror file written", repr(e))
if first == "Roblox keepalive: " + REASON:
    ok("D2 mirror first line carries the keepalive reason")
else:
    bad("D2 mirror first line carries the keepalive reason", f"got {first!r}")
log = read_log(st)
if log.count("DEGRADED: Roblox keepalive backed off") == 1 and "srt1" in log:
    ok("D3 exactly one DEGRADED log line, instance named")
else:
    bad("D3 exactly one DEGRADED log line, instance named", repr(log[-200:]))

print("== E. no log spam on repeat ==")
install(fake)
agent.sw_check_keepalive_degraded("srt1", st_dir=st, vol_base=vol)
restore()
log2 = read_log(st)
if log2.count("DEGRADED: Roblox keepalive backed off") == 1:
    ok("E1 second pass adds no repeat log line")
else:
    bad("E1 second pass adds no repeat log line", f"count={log2.count('DEGRADED: Roblox keepalive backed off')}")

print("== F. pre-existing marker never clobbered ==")
st, vol, fake = make_env(keepalive_marker=REASON + "\n")
mp = os.path.join(st, "srt1.degraded")
open(mp, "w").write("wd-srt1 restarted 7 times in last 3600s - auto-recovery paused\n")
install(fake)
r = agent.sw_check_keepalive_degraded("srt1", st_dir=st, vol_base=vol)
restore()
first = open(mp).readline().strip()
if r == "degraded" and first.startswith("wd-srt1 restarted 7 times"):
    ok("F1 circuit-breaker marker preserved, still 'degraded'")
else:
    bad("F1 circuit-breaker marker preserved", f"r={r!r} first={first!r}")
if "Roblox keepalive backed off" not in read_log(st):
    ok("F2 no DEGRADED log line when the marker was already there")
else:
    bad("F2 no DEGRADED log line when the marker was already there")

print("== G. empty/long reasons ==")
st, vol, fake = make_env(keepalive_marker="")
install(fake)
r = agent.sw_check_keepalive_degraded("srt1", st_dir=st, vol_base=vol)
restore()
first = open(os.path.join(st, "srt1.degraded")).readline().strip()
if r == "degraded" and first == "Roblox keepalive: launch attempts over budget":
    ok("G1 empty marker -> default reason")
else:
    bad("G1 empty marker -> default reason", f"r={r!r} first={first!r}")
long_reason = "x" * 300
st, vol, fake = make_env(keepalive_marker=long_reason + "\n")
install(fake)
agent.sw_check_keepalive_degraded("srt1", st_dir=st, vol_base=vol)
restore()
first = open(os.path.join(st, "srt1.degraded")).readline().rstrip("\n")
if len(first) <= len("Roblox keepalive: ") + 160 and first.startswith("Roblox keepalive: xxx"):
    ok("G2 long reason truncated to 160 on the marker line")
else:
    bad("G2 long reason truncated to 160", f"len={len(first)}")

print("== H. selfwatch_check_instance wiring ==")
st, vol, fake = make_env(keepalive_marker=REASON + "\n")
install(fake)
healthy = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
restore()
if healthy is False:
    ok("H1 marker present -> instance unhealthy (auto-clear waits)")
else:
    bad("H1 marker present -> instance unhealthy", f"got {healthy!r}")
if os.path.exists(os.path.join(st, "srt1.degraded")):
    ok("H2 mirror written by the full pass")
else:
    bad("H2 mirror written by the full pass")
st, vol, fake = make_env()
install(fake)
healthy = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
restore()
if healthy is True:
    ok("H3 no marker -> instance healthy, no mirror written")
else:
    bad("H3 no marker -> instance healthy", f"got {healthy!r}")
if not os.path.exists(os.path.join(st, "srt1.degraded")):
    ok("H4 healthy pass writes nothing")
else:
    bad("H4 healthy pass writes nothing")

print("== I. recovery auto-clears the mirror ==")
st, vol, fake = make_env(keepalive_marker=REASON + "\n")
install(fake)
agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
# the keepalive clears its own marker when Roblox is focused again
os.remove(os.path.join(vol, "wd-srt1-data", "_data", "data", "media", "0",
                       "jerusalem", "keepalive.degraded"))
healthy = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
restore()
if healthy is True and not os.path.exists(os.path.join(st, "srt1.degraded")):
    ok("I1 keepalive recovery -> mirror auto-cleared, healthy again")
else:
    bad("I1 keepalive recovery -> mirror auto-cleared",
        f"healthy={healthy!r} marker={os.path.exists(os.path.join(st, 'srt1.degraded'))}")
if "recovered from DEGRADED" in read_log(st):
    ok("I2 recovery logged")
else:
    bad("I2 recovery logged")

print("== J. invalid/unreadable -> never raises ==")
st, vol, fake = make_env()
install(fake)
try:
    for n in ("../x", "", None, "a" * 33):
        r = agent.sw_check_keepalive_degraded(n, st_dir=st, vol_base=vol)
        if r != "skip":
            bad("J1 invalid name -> 'skip'", f"{n!r} -> {r!r}")
    ok("J1 invalid names -> 'skip'")
    r = agent.sw_check_keepalive_degraded("srt1", st_dir=st,
                                          vol_base="/nonexistent-vol")
    if r == "ok":
        ok("J2 unreadable volume -> 'ok', no raise")
    else:
        bad("J2 unreadable volume -> 'ok'", f"got {r!r}")
    r = agent.sw_check_keepalive_degraded("srt1", st_dir="/nonexistent-st",
                                          vol_base=vol)
    if r in ("ok", "degraded", "skip"):
        ok("J3 unwritable st_dir tolerated")
    else:
        bad("J3 unwritable st_dir tolerated", f"got {r!r}")
except Exception as e:
    bad("J invalid/unreadable -> never raises", repr(e))
restore()

print("== K. wiring contract ==")
src = inspect.getsource(agent.selfwatch_check_instance)
i_3b = src.find("sw_check_keepalive_degraded")
i_3 = src.find("# 3. screencap fresh?")
i_4 = src.find("# 4. degraded auto-clear")
if i_3 != -1 and i_3b != -1 and i_4 != -1 and i_3 < i_3b < i_4:
    ok("K1 3b runs after the stale-stream check, before the auto-clear")
else:
    bad("K1 3b placement", f"3={i_3} 3b={i_3b} 4={i_4}")
seg = src[i_3b:i_4] if i_3b != -1 and i_4 != -1 else ""
if 'if kd == "degraded":' in seg and "healthy = False" in seg:
    ok("K2 degraded -> healthy=False")
else:
    bad("K2 degraded -> healthy=False")
# 3c install-guard's own comment mentions cb_guard in prose (never a call);
# scope to the 3b block proper and check real budget calls only.
seg_3b = seg[:seg.find("# 3c.")] if "# 3c." in seg else seg
if "_sw_cb_guard(" not in seg_3b:
    ok("K3 surfacing only: no circuit-breaker budget touched")
else:
    bad("K3 surfacing only: no circuit-breaker budget touched")
if 'kd = "skip"' in seg:
    ok("K4 raising check degrades to skip, never breaks the pass")
else:
    bad("K4 raising check degrades to skip")

print("== L. dashboard visibility ==")
st, vol, fake = make_env(keepalive_marker=REASON + "\n")
install(fake)
agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
restore()
d = agent.selfwatch_state(st_dir=st)["degraded"]
if d.get("srt1", "").startswith("Roblox keepalive: Roblox launch failing"):
    ok("L1 selfwatch_state degraded map carries the mirror reason")
else:
    bad("L1 selfwatch_state degraded map carries the mirror reason", repr(d))
if "selfwatch_state()" in inspect.getsource(agent.build_heartbeat_payload):
    ok("L2 heartbeat payload wired to selfwatch_state() (Nodes badge, no manager change)")
else:
    bad("L2 heartbeat payload wired to selfwatch_state()")

print("== M. constants ==")
if agent.SW_KEEPALIVE_DEGRADED == "keepalive.degraded":
    ok("M1 SW_KEEPALIVE_DEGRADED == 'keepalive.degraded'")
else:
    bad("M1 SW_KEEPALIVE_DEGRADED", repr(agent.SW_KEEPALIVE_DEGRADED))

restore()
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
