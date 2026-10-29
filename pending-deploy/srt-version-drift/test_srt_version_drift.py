#!/usr/bin/env python3
"""Tests for the wing agent 1.0.5 remote-node Roblox version-drift guard.

The main-node watchdog (df-watchdog.sh section 10) detects a Roblox client
drifted off the pinned build, but remote (srt) nodes had no equivalent: a
force-updated client there was invisible until someone tapped into the
instance. The agent's selfwatch thread now runs check_roblox_drift() every
60s (alert-only, no auto-remediation), writes <name>.roblox_drift markers,
and instance_status_linux() surfaces them so the manager's /api/instances
remote branch + the existing dashboard drift badge pick them up.

Covers:
  A. VERSION 1.0.5 + PINNED_ROBLOX/ROBLOX_PKG constants
  B. check_roblox_drift: drift marker write, ALERT dedup, new-version refresh,
     back-on-pinned clearing, absent/dumpsys-fail/container-down/unbooted
     skips, invalid names, docker raising -> None (never raises)
  C. DF_PINNED_ROBLOX env override honored
  D. read_roblox_drift: missing/empty/dir/traversal, 160-char truncation
  E. instance_status_linux carries roblox_drift/_reason (running + stopped)
  F. selfwatch_check_instance hook: drift detected, alert-only (healthy
     untouched, no .degraded written)
  G. manager clean_agent_drift/_reason sanitizers + remote-branch wiring
  H. dashboard drift badge applies to remote cards (no dashboard change needed)
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")
HTML = os.path.join(REPO, "manager", "static", "index.html")

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


agent = load("drift_agent", AGENT)
app = load("drift_app", APP)

PINNED = "2.740.931"


class FakeDocker:
    """Stand-in for agent.docker: records calls, simulates node state."""

    def __init__(self):
        self.calls = []
        self.up = set()          # cnames that are running
        self.dumpsys_rc = 0
        self.dumpsys_ver = None  # versionName to report; None -> no versionName line
        self.fail = False        # when True, every call raises
        self.session_alive = True  # waydroid session probe (agent 1.0.11 guard)

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if self.fail:
            raise RuntimeError("docker exploded")
        if args[0] == "ps":
            return 0, "\n".join(f"{n}\tUp 1 hour" for n in sorted(self.up))
        if args[0] == "exec":
            if "getprop" in args:
                return (0, "1\n") if self.session_alive else (1, "")
            if "dumpsys" in args:
                if self.dumpsys_rc != 0:
                    return self.dumpsys_rc, ""
                body = "package: name=com.roblox.client\n"
                if self.dumpsys_ver is not None:
                    body += f"  versionName={self.dumpsys_ver}\n"
                return 0, body
            if "pgrep" in args:
                return 0, "4242\n"
            return 0, ""
        return 0, ""


def mkvol(vb, name, booted=True):
    base = os.path.join(vb, "wd-" + name + "-data", "_data",
                        "data", "media", "0", "jerusalem")
    if booted:
        os.makedirs(base, exist_ok=True)
        open(os.path.join(base, "booted"), "w").close()


def fresh():
    fd = FakeDocker()
    old = agent.docker
    agent.docker = fd
    st = tempfile.mkdtemp(prefix="drift-st-")
    vb = tempfile.mkdtemp(prefix="drift-vb-")
    return fd, old, st, vb


def restore(old):
    agent.docker = old


def alert_count(st):
    try:
        with open(os.path.join(st, "selfwatch.log")) as fh:
            return sum("ALERT" in l and "DRIFT" in l for l in fh)
    except OSError:
        return 0


# ------------------------------------------------- A. version + constants
print("== A. version + constants ==")
if agent.VERSION in ("1.0.6", "1.0.7", "1.0.8", "1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"):
    ok("VERSION is 1.0.6+")
else:
    bad("VERSION is 1.0.6+", repr(agent.VERSION))
if agent.PINNED_ROBLOX == PINNED:
    ok("PINNED_ROBLOX defaults to 2.740.931")
else:
    bad("PINNED_ROBLOX default", repr(agent.PINNED_ROBLOX))
if agent.ROBLOX_PKG == "com.roblox.client":
    ok("ROBLOX_PKG is com.roblox.client")
else:
    bad("ROBLOX_PKG", repr(agent.ROBLOX_PKG))

# ------------------------------------------------- B. drift lifecycle
print("== B. check_roblox_drift lifecycle ==")
fd, old, st, vb = fresh()
try:
    fd.up.add("wd-srt1")
    fd.dumpsys_ver = "2.741.100"
    mkvol(vb, "srt1")
    r = agent.check_roblox_drift("srt1", st_dir=st, vol_base=vb)
    if r is True:
        ok("drifted -> True")
    else:
        bad("drifted -> True", repr(r))
    mp = os.path.join(st, "srt1.roblox_drift")
    try:
        reason = open(mp).read()
    except OSError:
        reason = ""
    if "2.741.100" in reason and PINNED in reason:
        ok("marker reason names installed + pinned versions")
    else:
        bad("marker reason versions", repr(reason[:80]))
    if reason.splitlines() and len(reason.splitlines()[0]) <= 200:
        ok("reason is a single short line")
    else:
        bad("reason line shape")
    if alert_count(st) == 1:
        ok("one ALERT log line on first drift")
    else:
        bad("ALERT dedup (first)", str(alert_count(st)))
    # same version again -> no new ALERT
    agent.check_roblox_drift("srt1", st_dir=st, vol_base=vb)
    if alert_count(st) == 1:
        ok("no repeat ALERT for the same drifted version")
    else:
        bad("no repeat ALERT", str(alert_count(st)))
    # new version -> new ALERT + marker refresh
    fd.dumpsys_ver = "2.742.5"
    agent.check_roblox_drift("srt1", st_dir=st, vol_base=vb)
    if alert_count(st) == 2 and "2.742.5" in open(mp).read():
        ok("new version -> new ALERT + marker refreshed")
    else:
        bad("new version refresh", f"alerts={alert_count(st)}")
    # back on pinned -> marker + state cleared, log line
    fd.dumpsys_ver = PINNED
    r = agent.check_roblox_drift("srt1", st_dir=st, vol_base=vb)
    gone = not os.path.exists(mp) and \
        not os.path.exists(os.path.join(st, "srt1.roblox_drift_seen"))
    try:
        logtxt = open(os.path.join(st, "selfwatch.log")).read()
    except OSError:
        logtxt = ""
    if r is False and gone and "back on pinned" in logtxt:
        ok("back on pinned clears marker + state, logs recovery")
    else:
        bad("back on pinned", f"r={r} gone={gone}")
    # pinned steady state -> no log spam
    n0 = len(logtxt)
    agent.check_roblox_drift("srt1", st_dir=st, vol_base=vb)
    logtxt2 = open(os.path.join(st, "selfwatch.log")).read()
    if len(logtxt2) == n0:
        ok("pinned steady state writes nothing")
    else:
        bad("pinned steady state spam")
finally:
    restore(old)

print("== B2. skip paths ==")
fd, old, st, vb = fresh()
try:
    fd.up.add("wd-srt1")
    mkvol(vb, "srt1")
    # package absent (dumpsys ok, no versionName)
    fd.dumpsys_ver = None
    r = agent.check_roblox_drift("srt1", st_dir=st, vol_base=vb)
    if r is None and not os.path.exists(os.path.join(st, "srt1.roblox_drift")):
        ok("package absent -> None, no marker")
    else:
        bad("package absent", repr(r))
    # dumpsys failure
    fd.dumpsys_rc = 1
    fd.dumpsys_ver = "2.741.100"
    r = agent.check_roblox_drift("srt1", st_dir=st, vol_base=vb)
    if r is None and not os.path.exists(os.path.join(st, "srt1.roblox_drift")):
        ok("dumpsys failure -> None, no marker")
    else:
        bad("dumpsys failure", repr(r))
    fd.dumpsys_rc = 0
    # container down -> docker exec never called
    fd.up.discard("wd-srt1")
    n_exec = len([c for c in fd.calls if c[0] == "exec"])
    r = agent.check_roblox_drift("srt1", st_dir=st, vol_base=vb)
    n_exec2 = len([c for c in fd.calls if c[0] == "exec"])
    if r is None and n_exec2 == n_exec:
        ok("container down -> None, no docker exec")
    else:
        bad("container down", repr(r))
    # not booted -> skipped
    fd.up.add("wd-srt1")
    os.remove(os.path.join(vb, "wd-srt1-data", "_data",
                           "data", "media", "0", "jerusalem", "booted"))
    n_exec = len([c for c in fd.calls if c[0] == "exec"])
    r = agent.check_roblox_drift("srt1", st_dir=st, vol_base=vb)
    n_exec2 = len([c for c in fd.calls if c[0] == "exec"])
    if r is None and n_exec2 == n_exec:
        ok("not booted -> None, no dumpsys")
    else:
        bad("not booted", repr(r))
    # invalid name
    r = agent.check_roblox_drift("../../etc", st_dir=st, vol_base=vb)
    if r is None and not os.path.exists(os.path.join(st, "..", "etc.roblox_drift")):
        ok("invalid name skipped")
    else:
        bad("invalid name", repr(r))
    # docker raising -> None, never raises
    fd.fail = True
    mkvol(vb, "srt1")
    try:
        r = agent.check_roblox_drift("srt1", st_dir=st, vol_base=vb)
        if r is None:
            ok("docker exception -> None, never raises")
        else:
            bad("docker exception", repr(r))
    except Exception as e:  # noqa: BLE001
        bad("docker exception raised", str(e))
finally:
    restore(old)

# ------------------------------------------------- C. env override
print("== C. DF_PINNED_ROBLOX override ==")
fd, old, st, vb = fresh()
try:
    fd.up.add("wd-srt1")
    mkvol(vb, "srt1")
    saved = agent.PINNED_ROBLOX
    agent.PINNED_ROBLOX = "9.9.9"
    try:
        fd.dumpsys_ver = "9.9.9"
        r = agent.check_roblox_drift("srt1", st_dir=st, vol_base=vb)
        if r is False and not os.path.exists(os.path.join(st, "srt1.roblox_drift")):
            ok("override pinned match -> not drifted")
        else:
            bad("override pinned match", repr(r))
        fd.dumpsys_ver = PINNED
        r = agent.check_roblox_drift("srt1", st_dir=st, vol_base=vb)
        reason = open(os.path.join(st, "srt1.roblox_drift")).read()
        if r is True and "9.9.9" in reason:
            ok("override pinned mismatch -> drifted, reason names override")
        else:
            bad("override pinned mismatch", repr(r))
    finally:
        agent.PINNED_ROBLOX = saved
finally:
    restore(old)

# ------------------------------------------------- D. read_roblox_drift
print("== D. read_roblox_drift ==")
st = tempfile.mkdtemp(prefix="drift-rd-")
d, reason = agent.read_roblox_drift("srt1", st_dir=st)
if (d, reason) == (False, None):
    ok("missing marker -> (False, None)")
else:
    bad("missing marker", repr((d, reason)))
with open(os.path.join(st, "srt1.roblox_drift"), "w") as fh:
    fh.write("Roblox client 2.741.100 installed (pinned 2.740.931)\nsecond line\n")
d, reason = agent.read_roblox_drift("srt1", st_dir=st)
if d is True and reason == "Roblox client 2.741.100 installed (pinned 2.740.931)":
    ok("marker -> (True, first line only)")
else:
    bad("marker read", repr((d, reason)))
with open(os.path.join(st, "srt1.roblox_drift"), "w") as fh:
    fh.write("x" * 300 + "\n")
d, reason = agent.read_roblox_drift("srt1", st_dir=st)
if d is True and len(reason) == 160:
    ok("long reason truncated to 160")
else:
    bad("reason truncation", str(len(reason or "")))
open(os.path.join(st, "srt2.roblox_drift"), "w").close()
d, reason = agent.read_roblox_drift("srt2", st_dir=st)
if d is True and reason == "Roblox client drifted off pinned build":
    ok("empty marker -> generic reason")
else:
    bad("empty marker", repr((d, reason)))
os.mkdir(os.path.join(st, "srt3.roblox_drift"))
d, reason = agent.read_roblox_drift("srt3", st_dir=st)
if (d, reason) == (False, None):
    ok("directory named like marker -> (False, None)")
else:
    bad("directory marker", repr((d, reason)))
d, reason = agent.read_roblox_drift("../../srt1", st_dir=st)
if d is False:
    ok("traversal name sanitized")
else:
    bad("traversal name", repr((d, reason)))

# ------------------------------------------------- E. instance_status_linux
print("== E. instance_status_linux drift fields ==")
fd, old, st, vb = fresh()
try:
    fd.up.add("wd-srt1")
    mkvol(vb, "srt1")
    # drifted
    with open(os.path.join(st, "srt1.roblox_drift"), "w") as fh:
        fh.write("drift reason here\n")
    real_selfwatch = agent.SELF_WATCH_DIR
    agent.SELF_WATCH_DIR = st
    try:
        s = agent.instance_status_linux("srt1")
    finally:
        agent.SELF_WATCH_DIR = real_selfwatch
    if s.get("roblox_drift") is True and s.get("roblox_drift_reason") == "drift reason here":
        ok("running + drifted -> drift True + reason")
    else:
        bad("running drifted status", repr({k: s.get(k) for k in ("roblox_drift", "roblox_drift_reason")}))
    # clean
    os.remove(os.path.join(st, "srt1.roblox_drift"))
    agent.SELF_WATCH_DIR = st
    try:
        s = agent.instance_status_linux("srt1")
    finally:
        agent.SELF_WATCH_DIR = real_selfwatch
    if s.get("roblox_drift") is False and s.get("roblox_drift_reason") is None:
        ok("running + clean -> drift False/None")
    else:
        bad("running clean status", repr(s.get("roblox_drift")))
    # stopped
    fd.up.discard("wd-srt1")
    s = agent.instance_status_linux("srt1")
    if s.get("roblox_drift") is False and s.get("roblox_drift_reason") is None \
            and s.get("running") is False:
        ok("stopped -> drift False/None, running False")
    else:
        bad("stopped status", repr(s))
    # no regression on resource keys
    if "mem_used_mb" in s and "cpu_pct" in s:
        ok("status keeps mem/cpu keys")
    else:
        bad("status keys", repr(sorted(s.keys())))
finally:
    restore(old)

# ------------------------------------------------- F. selfwatch hook
print("== F. selfwatch hook (alert-only) ==")
fd, old, st, vb = fresh()
try:
    fd.up.add("wd-srt1")
    fd.dumpsys_ver = "2.741.100"
    mkvol(vb, "srt1")
    # The hook test isolates drift: give it a healthy stream, otherwise
    # the missing-frame guard (agent 1.0.42) correctly flags the pass
    # unhealthy for the missing live.png + missing helper scripts, which
    # is the stream check's business, not the drift hook's.
    _hook_base = os.path.join(vb, "wd-srt1-data", "_data", "data", "media",
                              "0", "jerusalem")
    for _s in ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh"):
        open(os.path.join(_hook_base, _s), "w").write("#!/bin/sh\n")
    open(os.path.join(_hook_base, "live.png"), "w").write("x")
    r = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vb)
    if r is True and os.path.exists(os.path.join(st, "srt1.roblox_drift")):
        ok("hook writes drift marker during the selfwatch pass")
    else:
        bad("hook marker", f"r={r}")
    if r is True and not os.path.exists(os.path.join(st, "srt1.degraded")):
        ok("hook is alert-only: healthy untouched, no .degraded")
    else:
        bad("hook alert-only", f"r={r}")
finally:
    restore(old)

# ------------------------------------------------- G. manager sanitizers + wiring
print("== G. manager sanitizers + remote branch ==")
if app.clean_agent_drift(True) is True:
    ok("clean_agent_drift(True) -> True")
else:
    bad("clean_agent_drift True")
if all(app.clean_agent_drift(v) is False for v in (False, None, 1, "true", [], {})):
    ok("clean_agent_drift(False/None/1/'true'/garbage) -> False")
else:
    bad("clean_agent_drift falsey")
if app.clean_agent_drift_reason(" drift! ") == "drift!":
    ok("clean_agent_drift_reason strips string")
else:
    bad("clean_agent_drift_reason strip")
if all(app.clean_agent_drift_reason(v) is None for v in (None, "", "   ", 123, True, [])):
    ok("clean_agent_drift_reason(None/empty/non-string) -> None")
else:
    bad("clean_agent_drift_reason none")
if app.clean_agent_drift_reason("y" * 200) is not None and \
        len(app.clean_agent_drift_reason("y" * 200)) == 160:
    ok("clean_agent_drift_reason truncates to 160")
else:
    bad("clean_agent_drift_reason truncation")
src = open(APP).read()
if '"roblox_drift": clean_agent_drift(rs.get("roblox_drift"))' in src and \
        'rs.get("roblox_drift_reason")' in src:
    ok("remote branch sanitizes agent drift fields")
else:
    bad("remote branch wiring")

# ------------------------------------------------- H. dashboard contract
print("== H. dashboard contract ==")
html = open(HTML).read()
if "i.roblox_drift" in html and "setup drift" in html and "i.roblox_drift_reason" in html:
    ok("drift badge keys on i.roblox_drift (remote cards included, no change needed)")
else:
    bad("dashboard drift badge")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
