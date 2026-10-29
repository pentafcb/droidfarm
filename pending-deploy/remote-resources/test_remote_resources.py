#!/usr/bin/env python3
"""Tests for remote-node live resource usage (wing agent 1.0.4 + manager).

Covers:
  A. agent.parse_size_mb units + garbage
  B. agent.parse_docker_stats: valid/garbage/non-wd/unsafe names
  C. agent.instance_resources_all: 15s cache (one docker call per window),
     docker failure keeps last-good data within TTL, beyond TTL -> {}
  D. agent.instance_status_linux: resource keys present on running (values or
     None), present-and-None on stopped; never raises
  E. manager.clean_agent_resource: valid/negative/huge/NaN/inf/garbage/None
  F. manager remote branch uses the cleaned agent numbers (source contract)
  G. dashboard: card usage condition still renders for remote instances
     (mem_used_mb + memory_mb); stale "remote nodes don't report" comment gone
  H. agent.VERSION == 1.0.4
"""
import importlib.util
import os
import re
import sys

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


def check(name, cond):
    if cond:
        ok(name)
    else:
        bad(name)


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("rr_agent", AGENT)
app = load("rr_app", APP)


def reset_res():
    agent._inst_res.update({"ts": 0.0, "ok_ts": 0.0, "data": {}})


# ---- A. parse_size_mb ----
print("== A. agent.parse_size_mb ==")
check("1.5GiB -> 1536", agent.parse_size_mb("1.5GiB") == 1536.0)
check("412.5MiB -> 412.5", agent.parse_size_mb("412.5MiB") == 412.5)
check("512KiB -> 0.5", agent.parse_size_mb("512KiB") == 0.5)
check("100B -> ~0", abs(agent.parse_size_mb("100B") - 100 / 1048576) < 1e-9)
check("lowercase mib ok", agent.parse_size_mb("2mib") == 2.0)
check("garbage -> None", agent.parse_size_mb("n/a") is None)
check("unknown unit -> None", agent.parse_size_mb("5XB") is None)
check("empty -> None", agent.parse_size_mb("") is None)

# ---- B. parse_docker_stats ----
print("== B. agent.parse_docker_stats ==")
SAMPLE = ("wd-srt1\t412.5MiB / 2GiB\t23.4%\n"
          "wd-srt2\t1.2GiB / 2GiB\t5%\n"
          "other\t10MiB / 1GiB\t1%\n"
          "wd-bad!name\t9MiB / 1GiB\t1%\n"
          "wd-srt3\tbroken\n"
          "wd-srt4\t9MiB / 1GiB\tnan%\n")
p = agent.parse_docker_stats(SAMPLE)
check("srt1 parsed", p.get("srt1") == {"mem_used_mb": 412.5, "cpu_pct": 23.4})
check("srt2 parsed (GiB)", p.get("srt2") == {"mem_used_mb": 1228.8, "cpu_pct": 5.0})
check("non-wd skipped", "other" not in p)
check("unsafe name skipped", "bad!name" not in p)
check("malformed line skipped", "srt3" not in p)
check("nan cpu skipped", "srt4" not in p)
check("empty -> {}", agent.parse_docker_stats("") == {})
check("None -> {}", agent.parse_docker_stats(None) == {})

# ---- C. instance_resources_all caching ----
print("== C. agent.instance_resources_all ==")


class FakeDocker:
    def __init__(self, out, rc=0):
        self.out = out
        self.rc = rc
        self.calls = 0

    def __call__(self, *args, timeout=60):
        self.calls += 1
        return self.rc, self.out


reset_res()
fd = FakeDocker("wd-srt1\t412.5MiB / 2GiB\t23.4%\n")
agent.docker = fd
r1 = agent.instance_resources_all()
r2 = agent.instance_resources_all()
check("first call parses", r1.get("srt1", {}).get("mem_used_mb") == 412.5)
check("second call within window: no new docker call", fd.calls == 1 and r2 == r1)

# expire the window -> refresh
agent._inst_res["ts"] = 0.0
fd.out = "wd-srt1\t500MiB / 2GiB\t10%\n"
r3 = agent.instance_resources_all()
check("expired window refetches", fd.calls == 2 and r3["srt1"]["mem_used_mb"] == 500.0)

# docker failure within TTL keeps last good data
agent._inst_res["ts"] = 0.0
fd.rc, fd.out = 1, "boom"
r4 = agent.instance_resources_all()
check("docker failure keeps last-good", r4.get("srt1", {}).get("mem_used_mb") == 500.0)

# docker failure beyond TTL -> {}
agent._inst_res["ts"] = 0.0
agent._inst_res["ok_ts"] = 0.0
r5 = agent.instance_resources_all()
check("stale data beyond TTL -> {}", r5 == {})

# never raises on docker raising
reset_res()


def boom(*a, **k):
    raise RuntimeError("docker exploded")


agent.docker = boom
try:
    check("docker raising -> {} not exception", agent.instance_resources_all() == {})
except Exception as e:
    bad("docker raising -> {} not exception", str(e))

# ---- D. instance_status_linux resource keys ----
print("== D. agent.instance_status_linux ==")
reset_res()
agent.docker = FakeDocker("wd-srt1\t412.5MiB / 2GiB\t23.4%\n")
agent.container_status = lambda cname: "stopped"  # noqa: E731
st = agent.instance_status_linux("srt1")
check("stopped: mem key present+None",
      "mem_used_mb" in st and st["mem_used_mb"] is None)
check("stopped: cpu key present+None",
      "cpu_pct" in st and st["cpu_pct"] is None)

agent.container_status = lambda cname: "running"  # noqa: E731
st = agent.instance_status_linux("srt1")
check("running: mem value", st.get("mem_used_mb") == 412.5)
check("running: cpu value", st.get("cpu_pct") == 23.4)

reset_res()
agent.docker = boom  # stats fail -> None keys, no exception
try:
    st = agent.instance_status_linux("srt1")
    check("running, stats failing: None keys, no raise",
          st.get("mem_used_mb") is None and st.get("cpu_pct") is None)
except Exception as e:
    bad("running, stats failing: None keys, no raise", str(e))

# ---- E. manager.clean_agent_resource ----
print("== E. manager.clean_agent_resource ==")
c = app.clean_agent_resource
check("int ok", c(412) == 412.0)
check("float string ok", c("23.45") == 23.4 or c("23.45") == 23.5)
check("rounding", c(23.44) == 23.4)
check("negative -> None", c(-1) is None)
check("huge -> None", c(1e9) is None)
check("NaN -> None", c(float("nan")) is None)
check("inf -> None", c(float("inf")) is None)
check("garbage -> None", c("abc") is None)
check("None -> None", c(None) is None)

# ---- F. manager remote branch uses agent numbers ----
print("== F. manager remote-branch contract ==")
src = open(APP).read()
api_inst = src.find('if path == "/api/instances":')
check("/api/instances handler found", api_inst > 0)
tail = src[api_inst:] if api_inst > 0 else ""
check("remote mem via clean_agent_resource",
      '"mem_used_mb": clean_agent_resource(rs.get("mem_used_mb"))' in tail)
check("remote cpu via clean_agent_resource",
      '"cpu_pct": clean_agent_resource(rs.get("cpu_pct"))' in tail)
inst_end = tail.find('m = re.fullmatch(r"/api/instances/([^/]+)/roblox"')
body = tail[:inst_end] if inst_end > 0 else tail
check("no hardcoded None pair in /api/instances",
      '"mem_used_mb": None, "cpu_pct": None' not in body)

# ---- G. dashboard contract ----
print("== G. dashboard contract ==")
html = open(HTML).read()
check("card usage renders when mem_used_mb + memory_mb",
      "if (i.mem_used_mb != null && i.memory_mb)" in html)
check("stale remote comment removed",
      "or on remote nodes)" not in html and "remote nodes don't report" not in html)

# ---- H. version ----
print("== H. version ==")
check("agent VERSION is 1.0.4+ (1.0.5 adds drift guard, 1.0.6 adds adaptive screencap, 1.0.7 adds node disk guard, 1.0.8 adds HTTP server bounds, 1.0.9 adds selfwatch-events endpoint)", agent.VERSION in ("1.0.4", "1.0.5", "1.0.6", "1.0.7", "1.0.8", "1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
