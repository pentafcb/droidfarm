#!/usr/bin/env python3
"""Tests for wing agent 1.0.3 node self-watchdog + manager selfwatch wiring.

Covers:
  A. agent.VERSION bump + manager version gate compat (>= 1.0.2 still passes)
  B. healthy instance -> no-op, no restarts, no markers
  C. dead helper daemon -> docker exec -d restart, recovery counted, logged
  D. circuit breaker: >8 helper restarts/10min -> DEGRADED marker + no more restarts
  E. no log spam while degraded (single DEGRADED line)
  F. degraded auto-clears when all checks green again
  G. dead container: 2 strikes -> docker start; start failure logged, no crash
  H. docker ps failure -> strike recorded, no exception
  I. stale live.png (>30s) -> pkill + loop restart; fresh png -> untouched;
     missing png (booted, loop alive) -> pkill + loop restart (missing-frame-guard)
  J. invalid instance names skipped; corrupt/expired event files tolerated
  K. selfwatch_state() summary shape (recoveries + degraded map)
  L. build_heartbeat_payload() carries the selfwatch summary
  M. manager: heartbeat stores sanitized selfwatch; /api/providers emits it
  N. dashboard: node card badge + Info modal auto-recovery line
"""
import importlib.util
import os
import re
import sys
import tempfile
import time

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


agent = load("sw_agent", AGENT)
app = load("sw_app", APP)


class FakeDocker:
    """Stand-in for agent.docker: records calls, simulates container state."""

    def __init__(self):
        self.calls = []
        self.containers = {}   # cname -> True (up)
        self.helpers = {}      # (cname, script) -> alive bool
        self.ps_rc = 0
        self.start_rc = 0
        self.session_alive = True  # getprop sys.boot_completed -> "1"

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[0] == "ps":
            if self.ps_rc != 0:
                return self.ps_rc, "Cannot connect to the Docker daemon"
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
            if self.start_rc != 0:
                return self.start_rc, "boom"
            self.containers[args[1]] = True
            return 0, ""
        return 0, ""

    def restarts(self):
        return [c for c in self.calls if c[0] == "exec" and "-d" in c]

    def read_log(self, st):
        p = os.path.join(st, "selfwatch.log")
        try:
            return open(p).read()
        except Exception:
            return ""


def make_env(all_alive=True, png_age=None):
    """Fresh tmp state dir + fake volume tree; returns (st, vol, fake).

    The instance is modeled as booted (the `booted` marker is always
    written): every scenario here is a running container, and the 1d
    stuck-boot check skips booted instances (the 1c session check owns
    them) — without the marker 1d would probe the fake's StartedAt and
    mark the pass unhealthy, which is not what these scenarios model.
    """
    st = tempfile.mkdtemp()
    vol = tempfile.mkdtemp()
    fake = FakeDocker()
    fake.containers["wd-srt1"] = True
    for s in ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh"):
        fake.helpers[("wd-srt1", s)] = all_alive
    d = os.path.join(vol, "wd-srt1-data", "_data", "data", "media", "0", "jerusalem")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "booted"), "w").write("1\n")
    # Normal production state: the helper scripts are installed in the data
    # volume (the missing-script case is owned by the wing-missing-script-guard
    # suite; without the files the 1.0.29 guard correctly skips restarts).
    for s in ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh"):
        open(os.path.join(d, s), "w").write("#!/bin/sh\n")
    if png_age is not None:
        png = os.path.join(d, "live.png")
        open(png, "w").write("x")
        ts = time.time() - png_age
        os.utime(png, (ts, ts))
    return st, vol, fake


orig_docker = agent.docker
orig_instances = agent.load_instances


def use_fake(fake, instances=None):
    agent.docker = fake
    agent.load_instances = lambda: instances if instances is not None else {"srt1": {}}


def restore():
    agent.docker = orig_docker
    agent.load_instances = orig_instances


print("== A. version bump + gate compat ==")
if agent.VERSION in ("1.0.3", "1.0.4", "1.0.5", "1.0.6", "1.0.7", "1.0.8", "1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"):  # 1.0.4 adds per-instance resources, 1.0.5 adds drift guard, 1.0.6 adds adaptive screencap, 1.0.7 adds node disk guard, 1.0.8 adds HTTP server bounds, 1.0.9 adds selfwatch-events endpoint, 1.0.11 adds session-death guard, 1.0.12 adds loop-device guard
    ok("VERSION is 1.0.3+")
else:
    bad("VERSION is 1.0.3+", repr(agent.VERSION))
if app._version_at_least("1.0.4", "1.0.2") and app._version_at_least("1.0.2", "1.0.2"):
    ok("manager gate accepts 1.0.4 and 1.0.2 for auto-login")
else:
    bad("manager gate accepts 1.0.4 and 1.0.2 for auto-login")

print("== B. healthy instance -> no-op ==")
st, vol, fake = make_env(png_age=5)
use_fake(fake)
try:
    healthy = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
if healthy is True:
    ok("healthy instance returns True")
else:
    bad("healthy instance returns True", repr(healthy))
if not fake.restarts():
    ok("no restart execs on healthy pass")
else:
    bad("no restart execs on healthy pass", str(fake.restarts()))
if not os.path.exists(os.path.join(st, "srt1.degraded")):
    ok("no degraded marker on healthy pass")
else:
    bad("no degraded marker on healthy pass")
if agent.selfwatch_state(st_dir=st) == {"recoveries": 0, "degraded": {}}:
    ok("selfwatch_state() empty summary on healthy pass")
else:
    bad("selfwatch_state() empty summary on healthy pass")

print("== C. dead helper -> restart + count + log ==")
st, vol, fake = make_env(png_age=5)
fake.helpers[("wd-srt1", "input-daemon.sh")] = False
use_fake(fake)
try:
    healthy = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
if healthy is False:
    ok("dead helper -> returns False (not all green)")
else:
    bad("dead helper -> returns False (not all green)")
r = fake.restarts()
if len(r) == 1 and r[0][1] == "-d" and r[0][2] == "wd-srt1" and "input-daemon.sh" in r[0][-1] \
        and "waydroid shell" in r[0][-1]:
    ok("restart is detached docker exec running waydroid shell for the script")
else:
    bad("restart is detached docker exec running waydroid shell for the script", str(r))
if agent.selfwatch_state(st_dir=st)["recoveries"] == 1:
    ok("recoveries counted")
else:
    bad("recoveries counted", repr(agent.selfwatch_state(st_dir=st)))
if "srt1 input-daemon.sh dead -> restart (1/8 per 10m)" in fake.read_log(st):
    ok("log line records the restart")
else:
    bad("log line records the restart")

print("== D. circuit breaker trips at >8/10min ==")
st, vol, fake = make_env(png_age=5)
fake.helpers[("wd-srt1", "input-daemon.sh")] = False
now = int(time.time())
with open(os.path.join(st, "srt1.helper-input_daemon_sh.ev"), "w") as f:
    f.write("\n".join(str(now - i * 30) for i in range(8)) + "\n")
use_fake(fake)
try:
    agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
mp = os.path.join(st, "srt1.degraded")
if not fake.restarts():
    ok("no restart once over budget")
else:
    bad("no restart once over budget", str(fake.restarts()))
reason = open(mp).readline().strip() if os.path.exists(mp) else ""
if os.path.exists(mp) and reason.startswith("input-daemon.sh restarted 9 times"):
    ok("degraded marker written, reason on first line")
else:
    bad("degraded marker written, reason on first line", repr(reason))
if "srt1 DEGRADED: input-daemon.sh restarted 9 times" in fake.read_log(st):
    ok("single DEGRADED log line")
else:
    bad("single DEGRADED log line")

print("== E. no spam while degraded ==")
use_fake(fake)
try:
    agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
log = fake.read_log(st)
if log.count("DEGRADED") == 1 and not fake.restarts():
    ok("second pass: no repeat log line, no restart attempts")
else:
    bad("second pass: no repeat log line, no restart attempts",
        f"degraded_lines={log.count('DEGRADED')} restarts={len(fake.restarts())}")

print("== F. degraded auto-clears when green ==")
for s in ("screencap-loop.sh", "input-daemon.sh", "roblox-keepalive.sh"):
    fake.helpers[("wd-srt1", s)] = True
use_fake(fake)
try:
    healthy = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
if healthy is True and not os.path.exists(mp) \
        and not [f for f in os.listdir(st) if f.endswith(".ev")]:
    ok("marker + event files cleared on all-green")
else:
    bad("marker + event files cleared on all-green")
if "recovered from DEGRADED" in fake.read_log(st):
    ok("recovery logged")
else:
    bad("recovery logged")

print("== G. dead container: 2 strikes -> docker start ==")
st, vol, fake = make_env(png_age=None)
del fake.containers["wd-srt1"]
use_fake(fake)
try:
    r1 = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
    r2 = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
starts = [c for c in fake.calls if c[0] == "start"]
if r1 is False and r2 is False and len(starts) == 1 and starts[0][1] == "wd-srt1":
    ok("first miss = strike only; second miss = docker start")
else:
    bad("first miss = strike only; second miss = docker start",
        f"r1={r1} r2={r2} starts={starts}")
if agent.selfwatch_state(st_dir=st)["recoveries"] == 1:
    ok("container restart counted")
else:
    bad("container restart counted")
# start failure path
st2, vol2, fake2 = make_env(png_age=None)
del fake2.containers["wd-srt1"]
fake2.start_rc = 1
use_fake(fake2)
try:
    agent.selfwatch_check_instance("srt1", st_dir=st2, vol_base=vol2)
    agent.selfwatch_check_instance("srt1", st_dir=st2, vol_base=vol2)
finally:
    restore()
if "docker start FAILED" in fake2.read_log(st2) \
        and agent.selfwatch_state(st_dir=st2)["recoveries"] == 0:
    ok("failed start logged, not counted, no crash")
else:
    bad("failed start logged, not counted, no crash")

print("== H. docker ps failure tolerated ==")
st, vol, fake = make_env(png_age=None)
fake.ps_rc = 1
use_fake(fake)
try:
    r = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
if r is False and os.path.exists(os.path.join(st, "srt1.norun")):
    ok("docker-down -> strike recorded, returns False, no exception")
else:
    bad("docker-down -> strike recorded, returns False, no exception")

print("== I. stale / fresh / missing live.png ==")
st, vol, fake = make_env(png_age=65)
use_fake(fake)
try:
    r = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
pk = [c for c in fake.calls if c[0] == "exec" and "pkill" in c]
rl = [c for c in fake.calls if c[0] == "exec" and "-d" in c and "screencap-loop" in c[-1]]
if r is False and pk and rl and "screencap stale (" in fake.read_log(st):
    ok("stale png -> pkill + loop restart + log")
else:
    bad("stale png -> pkill + loop restart + log", f"pkill={pk} restart={rl}")
st, vol, fake = make_env(png_age=5)
use_fake(fake)
try:
    agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
if not [c for c in fake.calls if c[0] == "exec" and ("pkill" in c or "-d" in c)]:
    ok("fresh png -> no pkill/restart")
else:
    bad("fresh png -> no pkill/restart")
st, vol, fake = make_env(png_age=None)
use_fake(fake)
try:
    r = agent.selfwatch_check_instance("srt1", st_dir=st, vol_base=vol)
finally:
    restore()
pk = [c for c in fake.calls if c[0] == "exec" and "pkill" in c]
rl = [c for c in fake.calls if c[0] == "exec" and "-d" in c and "screencap-loop" in c[-1]]
if r is False and pk and rl and "screencap missing (no live.png)" in fake.read_log(st):
    ok("missing png (booted, loop alive) -> pkill + loop restart + log")
else:
    bad("missing png (booted, loop alive) -> pkill + loop restart + log",
        f"r={r} pkill={pk} restart={rl}")

print("== J. names, event files ==")
st, vol, fake = make_env(png_age=None)
use_fake(fake)
try:
    r = agent.selfwatch_check_instance("../../etc/passwd", st_dir=st, vol_base=vol)
    r2 = agent.selfwatch_check_instance("", st_dir=st, vol_base=vol)
finally:
    restore()
if r is True and r2 is True and not fake.calls:
    ok("invalid names skipped, zero docker calls")
else:
    bad("invalid names skipped, zero docker calls")
with open(os.path.join(st, "k.ev"), "w") as f:
    f.write("garbage\n\n12x\n" + str(int(time.time())) + "\n")
if agent._sw_note_event(st, "k", 600) == 2:
    ok("corrupt event lines ignored")
else:
    bad("corrupt event lines ignored")
old = int(time.time()) - 700
with open(os.path.join(st, "w.ev"), "w") as f:
    f.write(str(old) + "\n" + str(old - 10) + "\n")
if agent._sw_note_event(st, "w", 600) == 1:
    ok("expired events pruned from sliding window")
else:
    bad("expired events pruned from sliding window")

print("== K. selfwatch_state() shape ==")
st, vol, fake = make_env(png_age=None)
with open(os.path.join(st, "recoveries.json"), "w") as f:
    f.write('{"total": 7}')
with open(os.path.join(st, "srt1.degraded"), "w") as f:
    f.write("y" * 300 + "\nsecond\n")
sw = agent.selfwatch_state(st_dir=st)
if sw["recoveries"] == 7 and sw["degraded"] == {"srt1": "y" * 160}:
    ok("recoveries + degraded map, reason truncated to 160")
else:
    bad("recoveries + degraded map, reason truncated to 160", repr(sw))

print("== L. build_heartbeat_payload() ==")
use_fake(fake, {"srt1": {}})
try:
    p = agent.build_heartbeat_payload()
finally:
    restore()
if p.get("version") in ("1.0.3", "1.0.4", "1.0.5", "1.0.6", "1.0.7", "1.0.8", "1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45") and p.get("instances") == ["srt1"] \
        and set(p.get("selfwatch", {}).keys()) == {"recoveries", "degraded"}:
    ok("payload carries version, instances, selfwatch summary")
else:
    bad("payload carries version, instances, selfwatch summary",
        repr({k: p.get(k) for k in ("version", "instances", "selfwatch")}))

print("== M. manager: heartbeat stores sanitized selfwatch; providers emit it ==")
src = open(APP).read()
if re.search(r'p\["selfwatch"\]\s*=\s*\{"recoveries": rec, "degraded": clean_deg', src):
    ok("heartbeat handler stores sanitized selfwatch on the provider")
else:
    bad("heartbeat handler stores sanitized selfwatch on the provider")
if re.search(r're\.sub\(r"\[\^A-Za-z0-9_-\]", "", str\(k\)\)', src):
    ok("degraded instance names sanitized")
else:
    bad("degraded instance names sanitized")
if re.search(r'sw = p\.get\("selfwatch"\)', src) and re.search(
        r'"selfwatch": sw if isinstance\(sw, dict\) else \{\}', src):
    ok("/api/providers emits selfwatch per node")
else:
    bad("/api/providers emits selfwatch per node")

print("== N. dashboard contracts ==")
html = open(HTML).read()
if "p.selfwatch.degraded" in html and re.search(r"needs attention|necesita atenci.n", html) \
        and 'title="' in html and "esc(swd[dnames[0]])" in html:
    ok("node card shows 'needs attention' badge with reason tooltip")
else:
    bad("node card shows 'needs attention' badge with reason tooltip")
if re.search(r"Auto-recover|Recuperaciones autom", html) and "p.selfwatch.recoveries" in html:
    ok("node Info modal shows auto-recovery count")
else:
    bad("node Info modal shows auto-recovery count")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
