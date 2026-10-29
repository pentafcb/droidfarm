#!/usr/bin/env python3
"""Tests for wing agent 1.0.7 node disk/log self-recovery.

Covers:
  A. agent.VERSION is 1.0.7
  B. sw_disk_usage_pct: real dir -> sane int; missing path -> None; never raises
  C. _sw_env_int: valid / garbage / missing env
  D. sw_check_disk healthy (<90): no docker calls, no markers, no log lines
  E. sw_check_disk unreadable disk -> silent no-op
  F. sw_check_disk >=90: prune order + safe args, log line, recovery bumped
  G. sw_check_disk prune failure: logged, no recovery bump
  H. budget: 3rd prune in an hour -> disk.degraded with reason, no docker calls
  I. degraded -> paused (WARN, no prune)
  J. relief (<90 with marker) -> marker + ev removed, resumed log
  K. sw_check_disk never raises (docker raising)
  L. sw_trim_container_logs: under-threshold untouched; over -> same inode,
     last KEEP bytes, log line, recovery bumped, event noted
  M. trim failure -> WARN, no raise
  N. trim_max/trim_keep overrides honored
  O. selfwatch_loop wiring: host guards called once per pass (source contract)
  P. selfwatch_state surfaces disk.degraded (reason truncated to 160)
  Q. constants (90 / 2 / glob shape)
  R. heartbeat payload still carries selfwatch (regression)
"""
import builtins
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


def check(name, cond, detail=""):
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


agent = load("ndg_agent", AGENT)


class FakeDocker:
    def __init__(self, prune_rc=0):
        self.calls = []
        self.prune_rc = prune_rc

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[:2] == ("image", "prune"):
            if self.prune_rc != 0:
                return self.prune_rc, "boom"
            return 0, "Total reclaimed space: 1.24GB\n"
        if args[:2] == ("builder", "prune"):
            return 0, ""
        return 0, ""


orig_docker = agent.docker
orig_usage = agent.sw_disk_usage_pct


def use(pct, fake):
    agent.docker = fake
    agent.sw_disk_usage_pct = lambda disk_path=agent.SW_DISK_PATH: pct


def restore():
    agent.docker = orig_docker
    agent.sw_disk_usage_pct = orig_usage


def read_log(st):
    try:
        return open(os.path.join(st, "selfwatch.log")).read()
    except Exception:
        return ""


def recoveries(st):
    try:
        import json
        return json.load(open(os.path.join(st, "recoveries.json")))["total"]
    except Exception:
        return 0


print("== A. version ==")
check("VERSION is 1.0.8+ (1.0.9 adds selfwatch-events endpoint)", agent.VERSION in ("1.0.8", "1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"), repr(agent.VERSION))

print("== B. sw_disk_usage_pct ==")
restore()
p = agent.sw_disk_usage_pct(tempfile.gettempdir())
check("real dir -> int in [0,100]", isinstance(p, int) and 0 <= p <= 100, repr(p))
check("missing path -> None", agent.sw_disk_usage_pct("/nonexistent-xyz-123") is None)
check("empty path -> None (never raises)", agent.sw_disk_usage_pct("") is None)

print("== C. _sw_env_int ==")
os.environ.pop("NDG_TEST_INT", None)
check("missing -> default", agent._sw_env_int("NDG_TEST_INT", 7) == 7)
os.environ["NDG_TEST_INT"] = "1234"
check("valid -> int", agent._sw_env_int("NDG_TEST_INT", 7) == 1234)
os.environ["NDG_TEST_INT"] = "garbage"
check("garbage -> default", agent._sw_env_int("NDG_TEST_INT", 7) == 7)
os.environ.pop("NDG_TEST_INT", None)

print("== D. healthy disk: no-op ==")
st = tempfile.mkdtemp()
fake = FakeDocker()
use(50, fake)
agent.sw_check_disk(st_dir=st)
check("no docker calls when <90", fake.calls == [], repr(fake.calls))
check("no disk.degraded marker", not os.path.exists(os.path.join(st, "disk.degraded")))
check("no disk log lines", "disk" not in read_log(st))
restore()

print("== E. unreadable disk: silent no-op ==")
st = tempfile.mkdtemp()
fake = FakeDocker()
use(None, fake)
try:
    agent.sw_check_disk(st_dir=st)
    check("returns cleanly", True)
except Exception as e:
    bad("returns cleanly", repr(e))
check("no docker calls", fake.calls == [])
check("no marker", not os.path.exists(os.path.join(st, "disk.degraded")))
restore()

print("== F. >=90: safe prune ==")
st = tempfile.mkdtemp()
fake = FakeDocker()
use(95, fake)
agent.sw_check_disk(st_dir=st)
check("image prune runs before builder prune",
      fake.calls[:2] == [("image", "prune", "-f"), ("builder", "prune", "-f")],
      repr(fake.calls))
toks = [t for c in fake.calls for t in c]
check("prune args never touch volumes or -a",
      not any(t == "volume" or t == "-a" for t in toks), repr(toks))
log = read_log(st)
check("log line reports before/after", "disk 95% -> safe prune" in log and "now 95%" in log, log[-200:])
check("log names reclaimed space", "Total reclaimed space" in log)
check("recovery counted", recoveries(st) == 1, str(recoveries(st)))
check("no degraded marker on first prune", not os.path.exists(os.path.join(st, "disk.degraded")))
restore()

print("== G. prune failure: logged, no recovery ==")
st = tempfile.mkdtemp()
fake = FakeDocker(prune_rc=1)
use(95, fake)
agent.sw_check_disk(st_dir=st)
log = read_log(st)
check("failure logged with rc", "rc=1" in log, log[-200:])
check("no recovery counted on failure", recoveries(st) == 0)
restore()

print("== H. budget: 3rd prune in an hour -> DEGRADED ==")
st = tempfile.mkdtemp()
fake = FakeDocker()
use(95, fake)
agent.sw_check_disk(st_dir=st)   # n=1 -> prune
agent.sw_check_disk(st_dir=st)   # n=2 -> prune
n_calls = len(fake.calls)
agent.sw_check_disk(st_dir=st)   # n=3 -> degraded, no prune
check("no docker calls on the budget-tripping pass", len(fake.calls) == n_calls,
      "%d -> %d" % (n_calls, len(fake.calls)))
mp = os.path.join(st, "disk.degraded")
check("disk.degraded written", os.path.isfile(mp))
reason = open(mp).readline() if os.path.isfile(mp) else ""
check("reason on first line names auto-prune paused", "auto-prune paused" in reason, reason[:80])
check("DEGRADED logged", "disk DEGRADED" in read_log(st))

print("== I. degraded -> paused ==")
before = len(fake.calls)
agent.sw_check_disk(st_dir=st)
check("no prune while degraded", len(fake.calls) == before)
check("paused WARN logged", "auto-prune paused (budget exhausted" in read_log(st))
restore()

print("== J. relief clears marker ==")
st = tempfile.mkdtemp()
open(os.path.join(st, "disk.degraded"), "w").write("old\n")
open(os.path.join(st, "disk.prune.ev"), "w").write("123\n")
fake = FakeDocker()
use(50, fake)
agent.sw_check_disk(st_dir=st)
check("marker removed on relief", not os.path.exists(os.path.join(st, "disk.degraded")))
check("event file removed on relief", not os.path.exists(os.path.join(st, "disk.prune.ev")))
check("'relieved' logged", "disk pressure relieved (50% on" in read_log(st))
check("no docker calls on relief", fake.calls == [])
restore()

print("== K. docker raising -> never raises ==")
st = tempfile.mkdtemp()


def boom(*a, **k):
    raise RuntimeError("docker down")


agent.docker = boom
agent.sw_disk_usage_pct = lambda disk_path=agent.SW_DISK_PATH: 95
try:
    agent.sw_check_disk(st_dir=st)
    check("returns cleanly", True)
except Exception as e:
    bad("returns cleanly", repr(e))
check("prune-error logged", "prune-error" in read_log(st))
restore()

print("== L. log trimmer ==")
st = tempfile.mkdtemp()
ld = tempfile.mkdtemp()
c1 = os.path.join(ld, "contABC")
os.makedirs(c1)
big = os.path.join(c1, "big-json.log")
data = bytes(range(256)) * 12  # 3072 bytes, distinct head/tail
open(big, "wb").write(data)
ino_before = os.stat(big).st_ino
small = os.path.join(c1, "small-json.log")
open(small, "wb").write(b"s" * 500)
agent.sw_trim_container_logs(st_dir=st, log_glob=os.path.join(ld, "*", "*-json.log"),
                             trim_max=1000, trim_keep=500)
check("under-threshold file untouched", os.path.getsize(small) == 500)
check("over-threshold trimmed to KEEP", os.path.getsize(big) == 500, str(os.path.getsize(big)))
check("kept bytes are the tail", open(big, "rb").read() == data[-500:])
check("same inode (fd-safe)", os.stat(big).st_ino == ino_before)
check("trim logged", "log-trim: trimmed" in read_log(st) and "contABC" in read_log(st))
check("recovery counted", recoveries(st) == 1)
check("log.trim event noted", os.path.isfile(os.path.join(st, "log.trim.ev")))

print("== M. trim failure -> WARN, no raise ==")
st = tempfile.mkdtemp()
ld = tempfile.mkdtemp()
c1 = os.path.join(ld, "contX")
os.makedirs(c1)
tgt = os.path.join(c1, "x-json.log")
open(tgt, "wb").write(b"z" * 2000)
real_open = builtins.open


def boom_open(p, *a, **k):
    if isinstance(p, str) and p.endswith("x-json.log"):
        raise IOError("denied")
    return real_open(p, *a, **k)


builtins.open = boom_open
try:
    agent.sw_trim_container_logs(st_dir=st, log_glob=os.path.join(ld, "*", "*-json.log"),
                                 trim_max=1000, trim_keep=500)
    check("returns cleanly", True)
except Exception as e:
    bad("returns cleanly", repr(e))
finally:
    builtins.open = real_open
check("WARN logged", "WARN log-trim: failed to trim" in read_log(st))
check("file untouched after failed trim", os.path.getsize(tgt) == 2000)

print("== N. threshold overrides ==")
st = tempfile.mkdtemp()
ld = tempfile.mkdtemp()
f1 = os.path.join(ld, "a-json.log")
open(f1, "wb").write(b"a" * 1500)
agent.sw_trim_container_logs(st_dir=st, log_glob=os.path.join(ld, "*-json.log"),
                             trim_max=2000, trim_keep=500)
check("trim_max override: 1500-byte file untouched", os.path.getsize(f1) == 1500)
agent.sw_trim_container_logs(st_dir=st, log_glob=os.path.join(ld, "*-json.log"),
                             trim_max=1000, trim_keep=100)
check("trim_keep override: trimmed to 100", os.path.getsize(f1) == 100)

print("== O. selfwatch_loop wiring (1.0.23: via selfwatch_pass_once) ==")
loop_src = inspect.getsource(agent.selfwatch_loop)
pass_src = inspect.getsource(agent.selfwatch_pass_once)
check("loop delegates to selfwatch_pass_once()",
      "selfwatch_pass_once()" in loop_src)
check("pass calls sw_check_disk() + sw_trim_container_logs()",
      "sw_check_disk" in pass_src
      and "sw_trim_container_logs" in pass_src)
check("host guards run after the per-instance loop",
      pass_src.find("for name in") < pass_src.find("sw_check_disk"))

print("== P. selfwatch_state surfaces disk.degraded ==")
st = tempfile.mkdtemp()
open(os.path.join(st, "disk.degraded"), "w").write("r" * 200 + "\nsecond line\n")
d = agent.selfwatch_state(st_dir=st)["degraded"]
check("disk key present", "disk" in d, repr(d))
check("reason truncated to 160", d.get("disk") == "r" * 160, str(len(d.get("disk", ""))))

print("== Q. constants ==")
check("threshold is 90", agent.SW_DISK_THRESHOLD_PCT == 90)
check("budget is 2/hour", agent.SW_DISK_PRUNE_BUDGET == 2)
check("glob targets docker json logs",
      "containers" in agent.SW_LOG_GLOB and "json.log" in agent.SW_LOG_GLOB)

print("== R. heartbeat still carries selfwatch ==")
p = agent.build_heartbeat_payload()
check("payload has selfwatch with recoveries/degraded",
      isinstance(p.get("selfwatch"), dict)
      and "recoveries" in p["selfwatch"] and "degraded" in p["selfwatch"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
