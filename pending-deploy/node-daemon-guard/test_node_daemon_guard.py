#!/usr/bin/env python3
"""Tests for wing agent 1.0.39: node docker-daemon guard (main-node
df-watchdog v15 section 0b parity).

The gap: a down dockerd looked exactly like dead containers — `docker ps`
fails fast, so selfwatch section 1 fired for EVERY instance (2 strikes ->
`docker start`, which also fails) and within the hour the 6/hour circuit
breaker marked EVERY instance DEGRADED and paused auto-recovery node-wide,
while the root cause (dead dockerd) was never repaired.

Covers:
  A. VERSION is 1.0.39 + parse ordering 1.0.39 > 1.0.38
  B. healthy probe -> True, no strikes, no markers, no systemctl
  C. down probe -> False, strike 1, one log line, no restart yet
  D. second consecutive down -> systemctl restart docker once, strikes reset
  E. restart failure logged, strikes reset (no restart storm)
  F. 2/hour budget trip -> docker.degraded marker, no more restarts, no spam
  G. healthy probe after degraded -> marker + event file cleared, recovered log
  H. hung probe (rc 124) counts as down; docker() raising counts as down
  I. garbage strike file treated as 0
  J. pass_once integration: daemon down skips boot recovery + per-instance
     loop, disk guard called with docker_down=True, docker-independent
     guards still run; daemon healthy runs the full pass
  K. sw_check_disk(docker_down=True) at >=90%: WARN logged, no prune call,
     no budget event noted; relief path unchanged
  L. selfwatch_state surfaces the global docker.degraded marker
  M. source contracts: probe idiom, budget constants, no destructive cmds
"""
import importlib.util
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


agent = load("dg_agent", AGENT)


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.info_rc = 0       # docker info result: 0 = daemon answers
        self.raise_info = False

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args and args[0] == "info":
            if self.raise_info:
                raise RuntimeError("boom")
            return self.info_rc, "" if self.info_rc == 0 else "Cannot connect"
        if args and args[0] == "ps":
            return 0, ""
        if args and args[0] == "image":
            return 0, "Total reclaimed: 0B"
        if args and args[0] == "builder":
            return 0, ""
        return 0, ""


class FakeRun:
    """Stand-in for agent.run: records systemctl calls."""

    def __init__(self):
        self.calls = []
        self.systemctl_rc = 0

    def __call__(self, cmd, timeout=30, shell=False):
        self.calls.append(cmd)
        if cmd and cmd[0] == "systemctl":
            return self.systemctl_rc, ""
        return 0, ""


def fresh_env():
    tmp = tempfile.mkdtemp()
    st = os.path.join(tmp, "sw")
    os.makedirs(st)
    fd = FakeDocker()
    fr = FakeRun()
    orig_docker, orig_run = agent.docker, agent.run
    agent.docker, agent.run = fd, fr
    return tmp, st, fd, fr, orig_docker, orig_run


def restore(orig_docker, orig_run):
    agent.docker, agent.run = orig_docker, orig_run


def log_text(st):
    try:
        with open(os.path.join(st, "selfwatch.log")) as fh:
            return fh.read()
    except Exception:
        return ""


def marker(st, name):
    p = os.path.join(st, name)
    try:
        with open(p) as fh:
            return fh.read()
    except Exception:
        return None


print("== A. version ==")
check(agent.VERSION == "1.0.45", "A1 VERSION is exactly 1.0.45", repr(agent.VERSION))
check(agent.parse_agent_version("1.0.39") > agent.parse_agent_version("1.0.38"),
      "A2 parse_agent_version orders 1.0.39 > 1.0.38")

print("== B. healthy probe ==")
tmp, st, fd, fr, od, orr = fresh_env()
try:
    check(agent.sw_check_docker_daemon(st_dir=st) is True, "B1 healthy probe returns True")
    check(marker(st, "docker.daemondown") is None, "B2 no strike file on healthy probe")
    check(marker(st, "docker.degraded") is None, "B3 no degraded marker on healthy probe")
    check(not [c for c in fr.calls if c and c[0] == "systemctl"], "B4 no systemctl call on healthy probe")
    check(fd.calls == [("info",)], "B5 exactly one docker info probe", repr(fd.calls))
finally:
    restore(od, orr)

print("== C. single down probe ==")
tmp, st, fd, fr, od, orr = fresh_env()
try:
    fd.info_rc = 1
    check(agent.sw_check_docker_daemon(st_dir=st) is False, "C1 down probe returns False")
    check(marker(st, "docker.daemondown") == "1", "C2 strike file = 1")
    check("docker daemon unreachable (strike 1/2)" in log_text(st), "C3 one unreachable log line")
    check(not [c for c in fr.calls if c and c[0] == "systemctl"], "C4 no restart on first strike")
    check(marker(st, "docker.degraded") is None, "C5 no degraded marker on first strike")
finally:
    restore(od, orr)

print("== D. second consecutive down restarts dockerd ==")
tmp, st, fd, fr, od, orr = fresh_env()
try:
    fd.info_rc = 1
    agent.sw_check_docker_daemon(st_dir=st)
    check(agent.sw_check_docker_daemon(st_dir=st) is False, "D1 second down still returns False")
    check([c for c in fr.calls if c[:3] == ["systemctl", "restart", "docker"]],
          "D2 systemctl restart docker called once", repr(fr.calls))
    check(len([c for c in fr.calls if c and c[0] == "systemctl"]) == 1, "D3 exactly one systemctl call")
    check(marker(st, "docker.daemondown") is None, "D4 strikes reset after restart attempt")
    check("docker restarted ok" in log_text(st), "D5 restart-ok logged")
    check("docker daemon unreachable twice" in log_text(st), "D6 twice-logged")
finally:
    restore(od, orr)

print("== E. restart failure ==")
tmp, st, fd, fr, od, orr = fresh_env()
try:
    fd.info_rc = 1
    fr.systemctl_rc = 1
    agent.sw_check_docker_daemon(st_dir=st)
    agent.sw_check_docker_daemon(st_dir=st)
    check("docker restart FAILED" in log_text(st), "E1 restart failure logged")
    check(marker(st, "docker.daemondown") is None, "E2 strikes reset even on failed restart")
    check(marker(st, "docker.degraded") is None, "E3 no degraded marker on failed restart")
finally:
    restore(od, orr)

print("== F. budget trip -> degraded, no spam ==")
tmp, st, fd, fr, od, orr = fresh_env()
try:
    fd.info_rc = 1
    for _ in range(2):  # restart 1
        agent.sw_check_docker_daemon(st_dir=st)
    for _ in range(2):  # restart 2
        agent.sw_check_docker_daemon(st_dir=st)
    check(len([c for c in fr.calls if c and c[0] == "systemctl"]) == 2,
          "F1 two restarts within budget")
    for _ in range(2):  # third window: budget exhausted
        agent.sw_check_docker_daemon(st_dir=st)
    check(marker(st, "docker.degraded") is not None, "F2 docker.degraded marker written")
    check("docker daemon" in (marker(st, "docker.degraded") or ""), "F3 marker reason names docker daemon")
    check(len([c for c in fr.calls if c and c[0] == "systemctl"]) == 2,
          "F4 no third systemctl call after budget trip")
    n_deg = log_text(st).count("DEGRADED")
    for _ in range(4):  # more down passes: no repeat DEGRADED spam
        agent.sw_check_docker_daemon(st_dir=st)
    check(log_text(st).count("DEGRADED") == n_deg, "F5 no repeat DEGRADED log spam",
          f"before={n_deg} after={log_text(st).count('DEGRADED')}")
    check(len([c for c in fr.calls if c and c[0] == "systemctl"]) == 2,
          "F6 still no restart while degraded")
finally:
    restore(od, orr)

print("== G. recovery clears degraded ==")
tmp, st, fd, fr, od, orr = fresh_env()
try:
    fd.info_rc = 1
    for _ in range(6):
        agent.sw_check_docker_daemon(st_dir=st)
    check(marker(st, "docker.degraded") is not None, "G1 precondition: degraded marker present")
    fd.info_rc = 0
    check(agent.sw_check_docker_daemon(st_dir=st) is True, "G2 healthy probe returns True")
    check(marker(st, "docker.degraded") is None, "G3 degraded marker cleared")
    check(not os.path.exists(os.path.join(st, "docker.restart.ev")), "G4 restart event file cleared")
    check("recovered from DEGRADED" in log_text(st), "G5 recovered log line")
finally:
    restore(od, orr)

print("== H. hung probe / raising docker ==")
tmp, st, fd, fr, od, orr = fresh_env()
try:
    fd.info_rc = 124
    check(agent.sw_check_docker_daemon(st_dir=st) is False, "H1 rc 124 (timeout) counts as down")
    check(marker(st, "docker.daemondown") == "1", "H2 strike recorded for hung probe")
finally:
    restore(od, orr)
tmp, st, fd, fr, od, orr = fresh_env()
try:
    fd.raise_info = True
    check(agent.sw_check_docker_daemon(st_dir=st) is False, "H3 docker() raising counts as down")
    check(marker(st, "docker.daemondown") == "1", "H4 strike recorded for raising docker")
finally:
    restore(od, orr)

print("== I. garbage strike file ==")
tmp, st, fd, fr, od, orr = fresh_env()
try:
    fd.info_rc = 1
    with open(os.path.join(st, "docker.daemondown"), "w") as fh:
        fh.write("not-a-number")
    agent.sw_check_docker_daemon(st_dir=st)
    check(marker(st, "docker.daemondown") == "1", "I1 garbage strike treated as 0 -> 1")
finally:
    restore(od, orr)

print("== J. pass_once integration ==")
tmp, st, fd, fr, od, orr = fresh_env()
try:
    orig_boot = agent.sw_boot_recovery
    orig_check = agent.selfwatch_check_instance
    orig_disk = agent.sw_check_disk
    orig_trim = agent.sw_trim_container_logs
    orig_loop = agent.sw_ensure_loop_devices
    orig_mem = agent.sw_check_memory
    orig_http = agent.sw_check_agent_http
    orig_load = agent.load_instances
    calls = {"boot": 0, "inst": [], "disk": [], "trim": 0, "loop": 0, "mem": 0}
    agent.sw_boot_recovery = lambda **k: (calls.__setitem__("boot", calls["boot"] + 1), "ok")[1]
    agent.selfwatch_check_instance = lambda name, **k: calls["inst"].append(name) or True
    agent.sw_check_disk = lambda **k: calls["disk"].append(k)
    agent.sw_trim_container_logs = lambda **k: calls.__setitem__("trim", calls["trim"] + 1)
    agent.sw_ensure_loop_devices = lambda **k: calls.__setitem__("loop", calls["loop"] + 1)
    agent.sw_check_memory = lambda **k: calls.__setitem__("mem", calls["mem"] + 1)
    agent.sw_check_agent_http = lambda *a, **k: "ok"
    agent.load_instances = lambda: {"srt1": {}, "srt2": {}}
    fd.info_rc = 1
    state = agent.selfwatch_pass_once(lock_fn=lambda: ("held", {"st_dir": st}))
    check(state == "held", "J1 pass returns held state")
    check(calls["boot"] == 0, "J2 boot recovery skipped while daemon down")
    check(calls["inst"] == [], "J3 per-instance loop skipped while daemon down")
    check(calls["disk"] == [{"docker_down": True}], "J4 disk guard called with docker_down=True",
          repr(calls["disk"]))
    check(calls["trim"] == 1 and calls["loop"] == 1 and calls["mem"] == 1,
          "J5 docker-independent guards still run", repr(calls))
    # healthy daemon: full pass runs
    calls.update({"boot": 0, "inst": [], "disk": [], "trim": 0, "loop": 0, "mem": 0})
    fd.info_rc = 0
    agent.selfwatch_pass_once(lock_fn=lambda: ("held", {"st_dir": st}))
    check(calls["boot"] == 1, "J6 boot recovery runs when daemon healthy")
    check(sorted(calls["inst"]) == ["srt1", "srt2"], "J7 per-instance loop runs when healthy")
    check(calls["disk"] == [{"docker_down": False}], "J8 disk guard called with docker_down=False")
    agent.sw_boot_recovery, agent.selfwatch_check_instance = orig_boot, orig_check
    agent.sw_check_disk, agent.sw_trim_container_logs = orig_disk, orig_trim
    agent.sw_ensure_loop_devices, agent.sw_check_memory = orig_loop, orig_mem
    agent.sw_check_agent_http, agent.load_instances = orig_http, orig_load
finally:
    restore(od, orr)

print("== K. disk gate while daemon down ==")
tmp, st, fd, fr, od, orr = fresh_env()
try:
    orig_pct = agent.sw_disk_usage_pct
    agent.sw_disk_usage_pct = lambda path: 95
    agent.sw_check_disk(st_dir=st, docker_down=True)
    check("auto-prune skipped (docker daemon down)" in log_text(st),
          "K1 WARN logged, prune skipped")
    check(fd.calls == [], "K2 no docker call at all while down", repr(fd.calls))
    check(not os.path.exists(os.path.join(st, "disk.prune.ev")), "K3 no budget event noted")
    agent.sw_disk_usage_pct = orig_pct
finally:
    restore(od, orr)

print("== L. degraded map surfaces docker.degraded ==")
tmp, st, fd, fr, od, orr = fresh_env()
try:
    with open(os.path.join(st, "docker.degraded"), "w") as fh:
        fh.write("docker daemon restarted 3 times - auto-recovery paused\n")
    state = agent.selfwatch_state(st_dir=st)
    check("docker" in state.get("degraded", {}), "L1 docker key in degraded map",
          repr(state.get("degraded")))
    check(state["degraded"]["docker"].startswith("docker daemon"),
          "L2 reason carried through")
finally:
    restore(od, orr)

print("== M. source contracts ==")
src = open(AGENT).read()
check('docker("info", timeout=15)' in src, "M1 probe is docker info, timeout-bounded")
check("SW_DOCKER_DAEMON_STRIKES = 2" in src and "SW_DOCKER_RESTART_BUDGET = 2" in src,
      "M2 strike/budget constants are 2/2 (watchdog v15 parity)")
check('["systemctl", "restart", "docker"]' in src, "M3 restart uses systemctl restart docker")
check("docker stop" not in src.split("def sw_check_docker_daemon")[1].split("def selfwatch_pass_once")[0]
      and "docker rm" not in src.split("def sw_check_docker_daemon")[1].split("def selfwatch_pass_once")[0]
      and "volume prune" not in src.split("def sw_check_docker_daemon")[1].split("def selfwatch_pass_once")[0],
      "M4 guard never stops/removes containers or prunes volumes")
check("docker_down" in src and "sw_check_disk(docker_down=not daemon_ok)" in src,
      "M5 pass_once wires the disk gate")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
