#!/usr/bin/env python3
"""Tests for wing agent 1.0.13 memory-pressure guard + OOM surfacing.

The main node got memory-pressure alerting in df-watchdog v7 (2026-10-08),
but wing nodes had no equivalent: an OOM-pressure node had its containers
killed while the agent selfwatch just restarted them, burning the
container-start circuit budget until a misleading "...restarted N times"
DEGRADED, with the real cause (memory pressure) never surfaced.

This suite covers:

  A. agent.VERSION is exactly 1.0.13 + manager >=1.0.2 gate compatibility
  B. sw_read_mem_kb units (parse, missing, malformed, unreadable)
  C. sw_oom_count units (case-insensitive, none, empty)
  D. healthy host -> no-op (no marker, no event file, no log lines)
  E. low readings x1/x2 -> WARN only, no marker; x3 -> mem.degraded marker
     (reason on first line) + single DEGRADED line (v7 parity)
  F. still low while degraded -> silent (no new log, no event growth)
  G. relief -> marker + event file removed, relief logged
  H. unreadable meminfo -> silent no-op (OOM scan still runs independently)
  I. OOM cursor: fresh delta -> one WARN with count; no delta -> silent;
     cursor advances monotonically in the normal case
  J. never raises (corrupt state dir, raising internals)
  K. selfwatch_loop wiring contract (host-level, once per pass, try/except)
  L. mem.degraded flows through selfwatch_state() (dashboard visibility)
  M. constants (v7 parity) + mem.oom event window

All tests pass dmesg_text explicitly so sw_oom_count never shells out to a
real dmesg in the hermetic suite.
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


agent = load("wmg_agent", AGENT)
app = load("wmg_app", APP)


def st_dir():
    return tempfile.mkdtemp(prefix="wmg-st-")


def log_text(d):
    p = os.path.join(d, "selfwatch.log")
    try:
        with open(p) as fh:
            return fh.read()
    except Exception:
        return ""


def write_meminfo(d, memkb):
    p = os.path.join(d, "meminfo")
    with open(p, "w") as fh:
        fh.write("MemTotal:       32123456 kB\n")
        fh.write("MemAvailable:   %d kB\n" % memkb)
    return p


# ---- A. version ------------------------------------------------------------
if agent.VERSION in ("1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"):
    ok("A1 VERSION is 1.0.13+")
else:
    bad("A1 VERSION is 1.0.13+", "got %r" % agent.VERSION)
# (Widened for 1.0.14; the exact-bump assertion now lives in the
# wing-stuck-boot suite, like the 1.0.12->1.0.13 precedent.)
if app._version_at_least("1.0.13", "1.0.2") and not app._version_at_least("1.0.1", "1.0.2"):
    ok("A2 manager >=1.0.2 gate accepts 1.0.13, rejects 1.0.1")
else:
    bad("A2 manager gate for 1.0.13")
if (agent.SW_MEM_LOW_KB, agent.SW_MEM_LOW_WINDOW, agent.SW_MEM_LOW_TRIPS,
        agent.SW_MEM_OOM_WINDOW) == (1048576, 3600, 3, 86400):
    ok("A3 constants 1048576/3600/3/86400 (v7 parity)")
else:
    bad("A3 constants", "got %r" % ((agent.SW_MEM_LOW_KB, agent.SW_MEM_LOW_WINDOW,
                                     agent.SW_MEM_LOW_TRIPS, agent.SW_MEM_OOM_WINDOW),))

# ---- B. sw_read_mem_kb ------------------------------------------------------
d = st_dir()
p = write_meminfo(d, 20971520)
if agent.sw_read_mem_kb(p) == 20971520:
    ok("B1 parses MemAvailable kB")
else:
    bad("B1 parses MemAvailable kB", "got %r" % agent.sw_read_mem_kb(p))
p2 = os.path.join(d, "noavail")
with open(p2, "w") as fh:
    fh.write("MemTotal:       32123456 kB\n")
if agent.sw_read_mem_kb(p2) is None:
    ok("B2 missing MemAvailable line -> None")
else:
    bad("B2 missing line", "got %r" % agent.sw_read_mem_kb(p2))
p3 = os.path.join(d, "garbage")
with open(p3, "w") as fh:
    fh.write("MemAvailable:   not-a-number kB\n")
if agent.sw_read_mem_kb(p3) is None:
    ok("B3 malformed value -> None")
else:
    bad("B3 malformed", "got %r" % agent.sw_read_mem_kb(p3))
if agent.sw_read_mem_kb(os.path.join(d, "does-not-exist")) is None:
    ok("B4 unreadable file -> None")
else:
    bad("B4 unreadable", "got a value")

# ---- C. sw_oom_count ---------------------------------------------------------
txt = ("[12345.6] Out of memory: Killed process 999 (waydroid) total-vm:1kB\n"
       "[12346.7] out of memory something else\n"
       "[12347.8] ordinary line\n")
if agent.sw_oom_count(txt) == 2:
    ok("C1 counts case-insensitively")
else:
    bad("C1 oom count", "got %r" % agent.sw_oom_count(txt))
if agent.sw_oom_count("") == 0:
    ok("C2 empty -> 0")
else:
    bad("C2 empty", "got %r" % agent.sw_oom_count(""))
if agent.sw_oom_count("nothing here\n") == 0:
    ok("C3 no matches -> 0")
else:
    bad("C3 no matches", "got %r" % agent.sw_oom_count("nothing here\n"))

# ---- D. healthy no-op --------------------------------------------------------
d = st_dir()
p = write_meminfo(d, 20971520)  # 20 GiB available
agent.sw_check_memory(st_dir=d, meminfo_path=p, dmesg_text="")
if not os.path.exists(os.path.join(d, "mem.degraded")):
    ok("D1 healthy: no mem.degraded")
else:
    bad("D1 healthy marker")
if not os.path.exists(os.path.join(d, "mem.low.ev")):
    ok("D2 healthy: no mem.low.ev")
else:
    bad("D2 healthy event file")
lt = log_text(d)
if "memory pressure" not in lt and "memory DEGRADED" not in lt and "OOM killer" not in lt:
    ok("D3 healthy: no memory log lines")
else:
    bad("D3 healthy log", repr(lt[:120]))

# ---- E. low readings: warn, then DEGRADED ------------------------------------
d = st_dir()
p = write_meminfo(d, 100000)  # ~98MB available, way below the floor
agent.sw_check_memory(st_dir=d, meminfo_path=p, dmesg_text="")
lt = log_text(d)
if "WARN memory pressure: MemAvailable 97MB (report only, 1/3 this hour)" in lt:
    ok("E1 first low reading -> WARN 1/3")
else:
    bad("E1 first WARN", repr(lt[:160]))
if not os.path.exists(os.path.join(d, "mem.degraded")):
    ok("E2 no marker after 1 low reading")
else:
    bad("E2 early marker")
agent.sw_check_memory(st_dir=d, meminfo_path=p, dmesg_text="")
lt = log_text(d)
if "2/3 this hour" in lt and not os.path.exists(os.path.join(d, "mem.degraded")):
    ok("E3 second low reading -> WARN 2/3, still no marker")
else:
    bad("E3 second WARN", repr(lt[-160:]))
agent.sw_check_memory(st_dir=d, meminfo_path=p, dmesg_text="")
mp = os.path.join(d, "mem.degraded")
if os.path.exists(mp):
    ok("E4 third low reading -> mem.degraded written")
else:
    bad("E4 marker missing after 3 lows")
with open(mp) as fh:
    reason = fh.readline().strip()
if reason.startswith("host MemAvailable") and "97MB" in reason:
    ok("E5 marker reason on first line names MemAvailable")
else:
    bad("E5 marker reason", repr(reason[:120]))
lt = log_text(d)
if lt.count("memory DEGRADED") == 1:
    ok("E6 exactly one DEGRADED log line")
else:
    bad("E6 DEGRADED log count", "count=%d" % lt.count("memory DEGRADED"))

# ---- F. silent while degraded ------------------------------------------------
before = log_text(d)
ev_before = open(os.path.join(d, "mem.low.ev")).read()
agent.sw_check_memory(st_dir=d, meminfo_path=p, dmesg_text="")
if log_text(d) == before:
    ok("F1 still-low while degraded -> no new log lines")
else:
    bad("F1 log grew while degraded")
if open(os.path.join(d, "mem.low.ev")).read() == ev_before:
    ok("F2 still-low while degraded -> event file untouched")
else:
    bad("F2 event file grew while degraded")

# ---- G. relief clears ---------------------------------------------------------
p_ok = write_meminfo(d, 20971520)
agent.sw_check_memory(st_dir=d, meminfo_path=p_ok, dmesg_text="")
if not os.path.exists(mp) and not os.path.exists(os.path.join(d, "mem.low.ev")):
    ok("G1 relief removes marker + event file")
else:
    bad("G1 relief cleanup")
if "memory pressure relieved" in log_text(d):
    ok("G2 relief logged")
else:
    bad("G2 relief log")

# ---- H. unreadable meminfo ---------------------------------------------------
d = st_dir()
agent.sw_check_memory(st_dir=d, meminfo_path=os.path.join(d, "nope"), dmesg_text="")
if not os.path.exists(os.path.join(d, "mem.degraded")) and "memory pressure" not in log_text(d):
    ok("H1 unreadable meminfo -> silent no-op (no marker, no log)")
else:
    bad("H1 unreadable meminfo", repr(log_text(d)[:120]))

# ---- I. OOM cursor -----------------------------------------------------------
d = st_dir()
p = write_meminfo(d, 20971520)
dmesg = ("[1] Out of memory: Killed process 1 (a)\n"
         "[2] Out of memory: Killed process 2 (b)\n"
         "[3] Out of memory: Killed process 3 (c)\n")
agent.sw_check_memory(st_dir=d, meminfo_path=p, dmesg_text=dmesg)
lt = log_text(d)
if "WARN OOM killer fired 3x since last check" in lt:
    ok("I1 fresh delta -> one WARN with the count")
else:
    bad("I1 OOM WARN", repr(lt[-200:]))
with open(os.path.join(d, "oom.count")) as fh:
    if fh.read().strip() == "3":
        ok("I2 cursor advances to 3")
    else:
        bad("I2 cursor", )
before = log_text(d)
agent.sw_check_memory(st_dir=d, meminfo_path=p, dmesg_text=dmesg)
if log_text(d) == before:
    ok("I3 no delta -> silent, no repeat WARN")
else:
    bad("I3 repeat WARN")
# newer dmesg, one more kill -> WARN fires with delta 1
agent.sw_check_memory(st_dir=d, meminfo_path=p,
                      dmesg_text=dmesg + "[4] OUT OF MEMORY: killed (d)\n")
if "WARN OOM killer fired 1x since last check" in log_text(d):
    ok("I4 later delta 1 -> WARN 1x")
else:
    bad("I4 later WARN")
# OOM scan works even when the host is memory-low (not gated on the mem path)
d2 = st_dir()
p2 = write_meminfo(d2, 100000)
agent.sw_check_memory(st_dir=d2, meminfo_path=p2, dmesg_text=dmesg)
if "OOM killer fired 3x" in log_text(d2):
    ok("I5 OOM scan runs on low-memory passes too")
else:
    bad("I5 OOM on low pass")

# ---- J. never raises ----------------------------------------------------------
d = st_dir()
file_as_dir = os.path.join(d, "not-a-dir")
open(file_as_dir, "w").close()  # makedirs + writes all fail
try:
    agent.sw_check_memory(st_dir=file_as_dir, meminfo_path=file_as_dir,
                          dmesg_text="")
    ok("J1 corrupt state dir -> no raise")
except Exception as e:
    bad("J1 corrupt state dir raised", repr(e))
orig = agent._sw_note_event
agent._sw_note_event = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    agent.sw_check_memory(st_dir=st_dir(), meminfo_path=write_meminfo(st_dir(), 100000),
                          dmesg_text="")
    ok("J2 raising internals -> no raise")
except Exception as e:
    bad("J2 raising internals", repr(e))
finally:
    agent._sw_note_event = orig

# ---- K. selfwatch wiring (1.0.23: guards live in selfwatch_pass_once) --------
src = inspect.getsource(agent.selfwatch_pass_once)
if "sw_check_memory" in src:
    ok("K1 selfwatch_pass_once calls sw_check_memory() once per pass")
else:
    bad("K1 wiring missing")
seg_start = src.find("agent 1.0.13")
if seg_start != -1 and "except Exception" in src[seg_start:seg_start + 400]:
    ok("K2 the call is try/except-wrapped (host-guard pattern)")
else:
    bad("K2 no try/except around the guard call")
if src.count("sw_check_memory") == 1:
    ok("K3 exactly one call site (not per instance)")
else:
    bad("K3 call sites", "count=%d" % src.count("sw_check_memory"))

# ---- L. dashboard visibility ---------------------------------------------------
d = st_dir()
mp = os.path.join(d, "mem.degraded")
reason_text = "host MemAvailable 97MB - instance limits overcommit RAM (needs human)"
with open(mp, "w") as fh:
    fh.write(reason_text + "\n")
st = agent.selfwatch_state(st_dir=d)
if st.get("degraded", {}).get("mem") == reason_text:
    ok("L1 mem.degraded reaches the selfwatch_state degraded map")
else:
    bad("L1 degraded map", repr(st.get("degraded")))
long_reason = "x" * 200
with open(mp, "w") as fh:
    fh.write(long_reason + "\n")
st = agent.selfwatch_state(st_dir=d)
if len(st["degraded"]["mem"]) <= 160:
    ok("L2 reason truncated to 160 chars")
else:
    bad("L2 truncation")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
