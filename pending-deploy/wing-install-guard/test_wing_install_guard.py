#!/usr/bin/env python3
"""Tests for wing agent 1.0.35 Roblox install-failure surfacing (main-node
df-watchdog.sh v15 check 3c parity).

REAL GAP: df-watchdog.sh v15 check 3c mirrors the in-container
boot-complete.sh's roblox_install_failed marker into the instance .degraded
marker, so the dashboard "needs attention" badge shows a failed Roblox
install. Wing agents (1.0.34) never did: the same waydroid-instance image
runs on wing nodes, so boot-complete.sh writes roblox_install_failed into
the data volume there too — but nothing ever surfaced it. A failed install
on a node instance sat on "installing_roblox" forever with a green card.
This suite covers:

  A. agent.VERSION is exactly 1.0.35 + manager >=1.0.2 gate compatibility
  B. sw_install_failed_marker_path: valid -> host-side volume path;
     invalid -> None; never raises
  C. no marker file -> "ok", no st_dir writes, no log spam
  D. marker present -> "degraded", <name>.degraded written with the
     "Roblox install failed: <reason>" first line, exactly one DEGRADED
     log line (first-line-only reason)
  E. no spam: second pass with marker still present adds no log line
  F. pre-existing <name>.degraded (other cause) is never clobbered
  G. empty marker file -> "unknown error" default; long reason truncated
     to 160 chars
  H. selfwatch_check_instance wiring: marker present -> unhealthy (False);
     absent -> healthy (True) on a fully-green fake node
  I. recovery: boot-complete clears its marker -> next pass auto-clears
     the mirror + logs "recovered"
  J. invalid names -> "skip"; unreadable volume/st_dir tolerated;
     never raises
  K. wiring contract: 3c block placement after 3b, before step 4;
     healthy=False on degraded; never routed through cb_guard
  L. mirror reason flows through selfwatch_state() degraded map (dashboard
     Nodes badge path, no manager/dashboard change)
  M. SW_INSTALL_FAILED_MARKER constant == "roblox_install_failed"
"""
import importlib.util
import inspect
import os
import sys
import tempfile

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


agent = load("ig_agent", AGENT)
app = load("ig_app", APP)

HELPERS = ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh")
REASON = "pm install failed: Failure [INSTALL_FAILED_INVALID_APK]"

MARKER_DIR = ("data", "media", "0", "jerusalem")


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


def make_env(booted=True, helpers_alive=True, install_failed_marker=None):
    st = tempfile.mkdtemp()
    vol = tempfile.mkdtemp()
    fake = FakeDocker()
    fake.containers["wd-srt1"] = True
    for s in HELPERS:
        fake.helpers[("wd-srt1", s)] = helpers_alive
    jd = os.path.join(vol, "wd-srt1-data", "_data", *MARKER_DIR)
    os.makedirs(jd)
    if booted:
        open(os.path.join(jd, "booted"), "w").write("1\n")
    # fresh live.png so the stale-stream check stays green
    open(os.path.join(jd, "live.png"), "w").write("png")
    if install_failed_marker is not None:
        open(os.path.join(jd, "roblox_install_failed"), "w").write(
            install_failed_marker)
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


def marker_dir(vol):
    return os.path.join(vol, "wd-srt1-data", "_data", *MARKER_DIR)


print("== A. version + gate ==")
if agent.VERSION in ("1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"):
    ok("A1 VERSION is 1.0.35+ (exact 1.0.37 owned by agent-body-cap)")
else:
    bad("A1 VERSION is 1.0.35+", f"got {agent.VERSION!r}")
if app._version_at_least("1.0.35", "1.0.2") and not app._version_at_least("1.0.1", "1.0.2"):
    ok("A2 manager >=1.0.2 gate accepts 1.0.35")
else:
    bad("A2 manager >=1.0.2 gate accepts 1.0.35")

print("== B. marker path ==")
st, vol, fake = make_env()
p = agent.sw_install_failed_marker_path("srt1", vol)
want = os.path.join(vol, "wd-srt1-data", "_data", *MARKER_DIR,
                    "roblox_install_failed")
if p == want:
    ok("B1 valid name -> host-side data-volume path")
else:
    bad("B1 valid name -> host-side data-volume path", f"got {p!r}")
try:
    n_bad = 0
    for n in ("../x", "", None, "a" * 33, "srt 1", "srt;1"):
        r = agent.sw_install_failed_marker_path(n, vol)
        if r is None:
            ok(f"B2 invalid name {n!r} -> None")
        else:
            bad("B2 invalid name -> None", f"{n!r} -> {r!r}")
            n_bad += 1
except Exception as e:
    bad("B2 invalid names never raise", repr(e))

print("== C. no marker -> ok, silent ==")
st, vol, fake = make_env()
install(fake)
r = agent.sw_check_install_failed("srt1", st_dir=st, vol_base=vol)
restore()
if r == "ok":
    ok("C1 no marker file -> 'ok'")
else:
    bad("C1 no marker file -> 'ok'", f"got {r!r}")
left = [f for f in os.listdir(st) if f != "selfwatch.log"]
if not left and read_log(st) == "":
    ok("C2 no st_dir writes, no log spam when healthy")
else:
    bad("C2 no st_dir writes, no log spam when healthy", f"left={left}")

print("== D. marker present -> degraded ==")
st, vol, fake = make_env(install_failed_marker=REASON + "\nsecond line never leaks\n")
install(fake)
r = agent.sw_check_install_failed("srt1", st_dir=st, vol_base=vol)
restore()
if r == "degraded":
    ok("D1 marker present -> 'degraded'")
else:
    bad("D1 marker present -> 'degraded'", f"got {r!r}")
mp = os.path.join(st, "srt1.degraded")
try:
    first = open(mp).readline().rstrip("\n")
except Exception as e:
    bad("D2 degraded marker written", repr(e))
    first = None
if first == "Roblox install failed: " + REASON:
    ok("D2 marker first line carries the install reason (first line only)")
else:
    bad("D2 marker first line carries the install reason", f"got {first!r}")
lines = [l for l in read_log(st).splitlines() if "DEGRADED" in l]
if len(lines) == 1 and "Roblox install failed" in lines[0]:
    ok("D3 exactly one DEGRADED log line")
else:
    bad("D3 exactly one DEGRADED log line", f"got {lines!r}")

print("== E. no repeat spam ==")
before = len(read_log(st).splitlines())
install(fake)
agent.sw_check_install_failed("srt1", st_dir=st, vol_base=vol)
restore()
if len(read_log(st).splitlines()) == before:
    ok("E1 second pass with marker present adds no log line")
else:
    bad("E1 second pass adds no log line")

print("== F. pre-existing marker never clobbered ==")
st, vol, fake = make_env(install_failed_marker=REASON + "\n")
open(os.path.join(st, "srt1.degraded"), "w").write(
    "input-daemon restarted 9 times\n")
install(fake)
r = agent.sw_check_install_failed("srt1", st_dir=st, vol_base=vol)
restore()
first = open(os.path.join(st, "srt1.degraded")).readline().rstrip("\n")
if r == "degraded" and first == "input-daemon restarted 9 times":
    ok("F1 pre-existing marker kept, still 'degraded'")
else:
    bad("F1 pre-existing marker kept", f"r={r!r} first={first!r}")

print("== G. defaults + truncation ==")
st, vol, fake = make_env(install_failed_marker="\n")
install(fake)
r = agent.sw_check_install_failed("srt1", st_dir=st, vol_base=vol)
restore()
first = open(os.path.join(st, "srt1.degraded")).readline().rstrip("\n")
if r == "degraded" and first == "Roblox install failed: unknown error":
    ok("G1 empty marker -> 'unknown error' default reason")
else:
    bad("G1 empty marker default", f"r={r!r} first={first!r}")
st, vol, fake = make_env(install_failed_marker="x" * 300 + "\n")
install(fake)
agent.sw_check_install_failed("srt1", st_dir=st, vol_base=vol)
restore()
first = open(os.path.join(st, "srt1.degraded")).readline().rstrip("\n")
if first == "Roblox install failed: " + "x" * 160:
    ok("G2 long reason truncated to 160 chars")
else:
    bad("G2 160-char truncation", f"len={len(first) if first else None}")

print("== H. selfwatch_check_instance wiring ==")
st, vol, fake = make_env(install_failed_marker=REASON + "\n")
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
st, vol, fake = make_env(install_failed_marker=REASON + "\n")
install(fake)
agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
# boot-complete clears its own marker on a later successful install run
os.remove(os.path.join(marker_dir(vol), "roblox_install_failed"))
healthy = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
restore()
if healthy is True and not os.path.exists(os.path.join(st, "srt1.degraded")):
    ok("I1 marker cleared -> mirror auto-cleared, healthy again")
else:
    bad("I1 mirror auto-cleared",
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
        r = agent.sw_check_install_failed(n, st_dir=st, vol_base=vol)
        if r != "skip":
            bad("J1 invalid name -> 'skip'", f"{n!r} -> {r!r}")
    ok("J1 invalid names -> 'skip'")
    r = agent.sw_check_install_failed("srt1", st_dir=st,
                                      vol_base="/nonexistent-vol")
    if r == "ok":
        ok("J2 unreadable volume -> 'ok', no raise")
    else:
        bad("J2 unreadable volume -> 'ok'", f"got {r!r}")
    r = agent.sw_check_install_failed("srt1", st_dir="/nonexistent-st",
                                      vol_base=vol)
    if r in ("ok", "degraded", "skip"):
        ok("J3 unwritable st_dir tolerated")
    else:
        bad("J3 unwritable st_dir tolerated", f"got {r!r}")
except Exception as e:
    bad("J invalid/unreadable never raises", repr(e))
finally:
    restore()

print("== K. wiring contract ==")
src = inspect.getsource(agent.selfwatch_check_instance)
if "# 3c." in src:
    ok("K1 3c check is marked in the pass")
else:
    bad("K1 3c check is marked in the pass")
i_3b = src.find("sw_check_keepalive_degraded(name")
i_3c = src.find("sw_check_install_failed(name")
i_4 = src.find("# 4. degraded auto-clear")
if i_3b != -1 and i_3c != -1 and i_4 != -1 and i_3b < i_3c < i_4:
    ok("K2 3c runs after 3b, before the step-4 auto-clear")
else:
    bad("K2 3c placement after 3b / before step 4",
        f"3b={i_3b} 3c={i_3c} step4={i_4}")
seg = src[src.find("# 3c."):src.find("# 4. degraded auto-clear")]
if "_sw_cb_guard(" not in seg:
    ok("K3 surfacing only: 3c never routes through the circuit breaker")
else:
    bad("K3 3c must not touch cb_guard")
if 'if im == "degraded":' in src and "healthy = False" in seg:
    ok("K4 degraded -> instance counts as unhealthy")
else:
    bad("K4 degraded -> unhealthy")

print("== L. degraded map visibility ==")
st, vol, fake = make_env(install_failed_marker=REASON + "\n")
install(fake)
agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
restore()
state = agent.selfwatch_state(st)
got = state.get("degraded", {}).get("srt1")
if got == "Roblox install failed: " + REASON:
    ok("L1 mirror reason flows through selfwatch_state() degraded map")
else:
    bad("L1 degraded map carries the mirror reason", f"got {got!r}")
st, vol, fake = make_env(install_failed_marker="y" * 300 + "\n")
install(fake)
agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
restore()
got = agent.selfwatch_state(st).get("degraded", {}).get("srt1")
if got is not None and len(got) <= 160:
    ok("L2 degraded-map reason is 160-capped")
else:
    bad("L2 degraded-map reason capped", f"len={len(got) if got else None}")

print("== M. constant ==")
if agent.SW_INSTALL_FAILED_MARKER == "roblox_install_failed":
    ok("M1 SW_INSTALL_FAILED_MARKER == 'roblox_install_failed'")
else:
    bad("M1 constant", f"got {agent.SW_INSTALL_FAILED_MARKER!r}")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
