#!/usr/bin/env python3
"""Tests for wing agent 1.0.10 background-thread supervisor.

Covers:
  A. agent.VERSION bump 1.0.9 -> 1.0.10
  B. register_supervised contract (entry shape, never raises)
  C. supervisor constants (30s / 3 restarts / 3600s window)
  D. first pass starts registered threads immediately (no sleep)
  E. healthy no-op: alive threads -> no factory call, no log spam
  F. dead thread -> restart via factory + WARN line
  G. circuit breaker: 4th death in window -> given_up + exactly one ALERT,
     5th pass silent (no restart storm, no log spam)
  H. sliding-window expiry: old restarts pruned -> restart allowed again
     (given_up stays sticky, like the manager)
  I. raising factory: caught, WARN logged, budget not burned, retried
  J. _asup_log format parseable + manager watchdog_event_severity tags the
     ALERT red / WARN amber (real Eventos-modal contract)
  K. corrupt registry entries never poison a pass
  L. main() wiring: no direct heartbeat/selfwatch .start(), registrations +
     supervisor daemon thread present
  M. _supervisor_registrations: unconfigured agent skips heartbeat, Windows
     skips selfwatch
  N. thread factories: unstarted daemon threads, right targets/names
  O. supervisor loop survives a raising _asupervise_once
  P. _asup_log never raises on an unwritable log dir
  Q. manager version gate (>=1.0.2) accepts 1.0.10
"""
import importlib.util
import os
import re
import sys
import tempfile
import threading
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


agent = load("asup_agent", AGENT)
app = load("asup_app", APP)

TMP = tempfile.mkdtemp(prefix="asup_")
agent.SELF_WATCH_DIR = TMP  # hermetic: supervisor log lines land here


def reset_registry():
    agent._ASUPERVISED.clear()


def log_lines():
    p = os.path.join(TMP, "selfwatch.log")
    if not os.path.exists(p):
        return []
    with open(p) as fh:
        return fh.read().splitlines()


def clear_log():
    p = os.path.join(TMP, "selfwatch.log")
    if os.path.exists(p):
        os.remove(p)


def dead_factory():
    """Factory whose thread dies immediately (for restart/circuit tests)."""
    calls = []

    def factory():
        calls.append(1)
        return threading.Thread(target=lambda: None, daemon=True)

    factory.calls = calls
    return factory


def live_factory():
    """Factory whose thread stays alive (for healthy no-op tests)."""
    def factory():
        return threading.Thread(target=lambda: time.sleep(60), daemon=True)

    return factory


# A. VERSION bump -------------------------------------------------------------
# Widened for 1.0.11 (session-death guard) and 1.0.12 (loop-device guard); the exact-bump assertion lives in
# the wing-session-guard suite now.
if agent.VERSION in ("1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"):
    ok("A1 VERSION is 1.0.10+")
else:
    bad("A1 VERSION is 1.0.10+", f"got {agent.VERSION!r}")

# B. register_supervised contract ---------------------------------------------
reset_registry()
try:
    agent.register_supervised("hb", dead_factory())
    e = agent._ASUPERVISED.get("hb")
    if (e and e["thread"] is None and e["restarts"] == []
            and e["given_up"] is False and callable(e["factory"])):
        ok("B1 register stores entry shape")
    else:
        bad("B1 register stores entry shape", f"{e!r}")
except Exception as ex:
    bad("B1 register stores entry shape", f"raised {ex!r}")

try:
    agent.register_supervised(None, None)
    agent.register_supervised("", dead_factory())
    ok("B2 register never raises on garbage")
except Exception as ex:
    bad("B2 register never raises on garbage", f"raised {ex!r}")

# C. constants ----------------------------------------------------------------
if (agent.ASUPERVISOR_INTERVAL, agent.ASUPERVISOR_MAX_RESTARTS,
        agent.ASUPERVISOR_WINDOW) == (30, 3, 3600):
    ok("C1 constants 30s / 3 restarts / 3600s window")
else:
    bad("C1 constants", f"{agent.ASUPERVISOR_INTERVAL}/{agent.ASUPERVISOR_MAX_RESTARTS}/{agent.ASUPERVISOR_WINDOW}")

# D. first pass starts immediately ---------------------------------------------
reset_registry()
clear_log()
f = live_factory()
agent.register_supervised("t1", f)
agent._asupervise_once()
t = agent._ASUPERVISED["t1"]["thread"]
if t is not None and t.is_alive():
    ok("D1 first pass starts registered thread immediately")
else:
    bad("D1 first pass starts registered thread immediately")

# E. healthy no-op -------------------------------------------------------------
reset_registry()
clear_log()
calls = []


def counting_live():
    def factory():
        calls.append(1)
        return threading.Thread(target=lambda: time.sleep(60), daemon=True)

    return factory


agent.register_supervised("t2", counting_live())
agent._asupervise_once()   # starts it (one start-WARN is expected)
n_calls = len(calls)
n_log = len(log_lines())
agent._asupervise_once()   # healthy pass: must not restart
if len(calls) == n_calls and len(log_lines()) == n_log:
    ok("E1 healthy pass: no factory call, no log spam")
else:
    bad("E1 healthy pass", f"calls {len(calls)} vs {n_calls}, log {log_lines()!r}")

# F. dead thread -> restart + WARN ------------------------------------------------
reset_registry()
clear_log()
f = dead_factory()
agent.register_supervised("t3", f)
agent._asupervise_once()          # starts (dies instantly)
t_first = agent._ASUPERVISED["t3"]["thread"]
agent._asupervise_once()          # dead -> restart
t_second = agent._ASUPERVISED["t3"]["thread"]
warns = [l for l in log_lines() if "WARN" in l and "t3" in l]
if (t_first is not t_second and t_second is not None
        and len(f.calls) == 2 and len(warns) >= 1
        and len(agent._ASUPERVISED["t3"]["restarts"]) == 2):
    ok("F1 dead thread restarted via factory + WARN logged")
else:
    bad("F1 dead thread restarted", f"calls={len(f.calls)} warns={warns!r}")

# G. circuit breaker -------------------------------------------------------------
reset_registry()
clear_log()
f = dead_factory()
agent.register_supervised("t4", f)
for _ in range(4):                # 3 restarts burn the budget, 4th gives up
    agent._asupervise_once()
e = agent._ASUPERVISED["t4"]
alerts = [l for l in log_lines() if "ALERT" in l and "t4" in l]
if e["given_up"] and len(f.calls) == 3 and len(alerts) == 1:
    ok("G1 4th death in window -> given_up + exactly one ALERT")
else:
    bad("G1 circuit trip", f"given_up={e['given_up']} calls={len(f.calls)} alerts={len(alerts)}")
n_calls = len(f.calls)
n_alerts = len(alerts)
agent._asupervise_once()          # 5th pass: silent
agent._asupervise_once()
alerts2 = [l for l in log_lines() if "ALERT" in l and "t4" in l]
if len(f.calls) == n_calls and len(alerts2) == n_alerts:
    ok("G2 post-give-up passes silent (no storm, no spam)")
else:
    bad("G2 post-give-up silent", f"calls {len(f.calls)} alerts {len(alerts2)}")

# H. sliding-window expiry ---------------------------------------------------------
reset_registry()
clear_log()
old = time.time() - 3700
f = dead_factory()
agent.register_supervised("t5", f)
agent._ASUPERVISED["t5"]["restarts"] = [old, old, old]
agent._asupervise_once()
if (len(f.calls) == 1
        and len(agent._ASUPERVISED["t5"]["restarts"]) == 1
        and not agent._ASUPERVISED["t5"]["given_up"]):
    ok("H1 expired restarts pruned -> restart allowed again")
else:
    bad("H1 window expiry", f"calls={len(f.calls)}")

# given_up is sticky even after the window expires (manager parity)
reset_registry()
f = dead_factory()
agent.register_supervised("t5b", f)
agent._ASUPERVISED["t5b"]["given_up"] = True
agent._ASUPERVISED["t5b"]["restarts"] = [time.time() - 3700]
agent._asupervise_once()
if len(f.calls) == 0 and agent._ASUPERVISED["t5b"]["given_up"]:
    ok("H2 given_up sticky after window expiry")
else:
    bad("H2 given_up sticky", f"calls={len(f.calls)}")

# I. raising factory ---------------------------------------------------------------
reset_registry()
clear_log()
calls = []


def raising_factory():
    calls.append(1)
    raise RuntimeError("boom")


agent.register_supervised("t6", raising_factory)
agent._asupervise_once()
warns = [l for l in log_lines() if "failed to (re)start" in l and "t6" in l]
e = agent._ASUPERVISED["t6"]
if (len(warns) == 1 and e["thread"] is None and e["restarts"] == []
        and not e["given_up"]):
    ok("I1 raising factory: WARN logged, budget not burned")
else:
    bad("I1 raising factory", f"warns={warns!r} restarts={e['restarts']!r}")
agent._asupervise_once()          # retried next pass, still not burned
if len(calls) == 2 and agent._ASUPERVISED["t6"]["restarts"] == []:
    ok("I2 raising factory retried next pass without burning budget")
else:
    bad("I2 raising factory retry", f"calls={len(calls)}")

# J. log format + manager severity contract -------------------------------------------
clear_log()
agent._asup_log("ALERT supervisor gave up restarting 'x' (3 restarts in the last hour) - needs human attention")
agent._asup_log("WARN supervisor started background thread 'y'")
lines = log_lines()
fmt_ok = all(re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ", l) for l in lines) and len(lines) == 2
if fmt_ok:
    ok("J1 _asup_log lines carry [YYYY-MM-DD HH:MM:SS] prefix")
else:
    bad("J1 _asup_log format", f"{lines!r}")
try:
    sev_alert = app.watchdog_event_severity(lines[0])
    sev_warn = app.watchdog_event_severity(lines[1])
    # also: the manager's _event_ts must parse the prefix (Eventos merge)
    ts = app._event_ts(lines[0])
    if sev_alert == "alert" and sev_warn == "warn" and ts:
        ok("J2 manager tags ALERT->alert / WARN->warn, _event_ts parses")
    else:
        bad("J2 manager severity contract", f"{sev_alert}/{sev_warn} ts={ts!r}")
except Exception as ex:
    bad("J2 manager severity contract", f"raised {ex!r}")

# K. corrupt registry entries never poison a pass ---------------------------------------
reset_registry()
clear_log()
agent._ASUPERVISED["bad1"] = {}
agent._ASUPERVISED["bad2"] = {"thread": "not-a-thread", "restarts": "garbage"}
agent._ASUPERVISED["bad3"] = None
f = live_factory()
agent.register_supervised("good", f)
try:
    agent._asupervise_once()
    t = agent._ASUPERVISED["good"]["thread"]
    if t is not None and t.is_alive():
        ok("K1 corrupt entries skipped, healthy entry still supervised")
    else:
        bad("K1 corrupt entries", "good entry not started")
except Exception as ex:
    bad("K1 corrupt entries", f"raised {ex!r}")

# L. main() wiring contract ------------------------------------------------------------------
src = open(AGENT).read()
main_src = src[src.index("def main():"):]
old_direct = "threading.Thread(target=heartbeat_loop, daemon=True)\n    t.start()" in main_src
if not old_direct:
    ok("L1 main() no longer starts heartbeat_loop directly")
else:
    bad("L1 main() no longer starts heartbeat_loop directly")
old_direct2 = "threading.Thread(target=selfwatch_loop, daemon=True)\n    t2.start()" in main_src
if not old_direct2:
    ok("L2 main() no longer starts selfwatch_loop directly")
else:
    bad("L2 main() no longer starts selfwatch_loop directly")
if ('register_supervised("heartbeat"' in main_src
        or "_supervisor_registrations()" in main_src):
    ok("L3 main() registers threads via _supervisor_registrations()")
else:
    bad("L3 main() registrations")
if 'name="agent-supervisor"' in main_src and "agent_thread_supervisor" in main_src:
    ok("L4 main() starts the supervisor daemon thread")
else:
    bad("L4 supervisor thread in main()")

# M. _supervisor_registrations edge cases ---------------------------------------------------------
real_main_url, real_pid, real_key = agent.MAIN_URL, agent.PROVIDER_ID, agent.AGENT_KEY
agent.MAIN_URL, agent.PROVIDER_ID, agent.AGENT_KEY = "", "", ""
regs = dict(agent._supervisor_registrations())
if "heartbeat" not in regs:
    ok("M1 unconfigured agent skips heartbeat registration")
else:
    bad("M1 unconfigured agent skips heartbeat")
agent.MAIN_URL, agent.PROVIDER_ID, agent.AGENT_KEY = "http://x", "p1", "k1"
regs = dict(agent._supervisor_registrations())
if "heartbeat" in regs and regs["heartbeat"] is agent._heartbeat_thread_factory:
    ok("M2 configured agent registers heartbeat")
else:
    bad("M2 configured agent registers heartbeat")
real_win = agent.IS_WINDOWS
agent.IS_WINDOWS = True
regs = dict(agent._supervisor_registrations())
if "selfwatch" not in regs:
    ok("M3 Windows skips selfwatch registration")
else:
    bad("M3 Windows skips selfwatch")
agent.IS_WINDOWS = real_win
agent.MAIN_URL, agent.PROVIDER_ID, agent.AGENT_KEY = real_main_url, real_pid, real_key
if not real_win and "selfwatch" in dict(agent._supervisor_registrations()):
    ok("M4 Linux registers selfwatch")
else:
    bad("M4 Linux registers selfwatch", f"IS_WINDOWS={real_win}")

# N. thread factories -------------------------------------------------------------------------------
t = agent._heartbeat_thread_factory()
if (isinstance(t, threading.Thread) and not t.is_alive() and t.daemon
        and t._target is agent.heartbeat_loop and t.name == "agent-heartbeat"):
    ok("N1 heartbeat factory: unstarted daemon thread, right target/name")
else:
    bad("N1 heartbeat factory", f"{t!r}")
t = agent._selfwatch_thread_factory()
if (isinstance(t, threading.Thread) and not t.is_alive() and t.daemon
        and t._target is agent.selfwatch_loop and t.name == "agent-selfwatch"):
    ok("N2 selfwatch factory: unstarted daemon thread, right target/name")
else:
    bad("N2 selfwatch factory", f"{t!r}")

# O. supervisor loop survives a raising pass ------------------------------------------------------------
orig_once = agent._asupervise_once
orig_interval = agent.ASUPERVISOR_INTERVAL
agent.ASUPERVISOR_INTERVAL = 0.05


def raising_once():
    raise RuntimeError("pass boom")


agent._asupervise_once = raising_once
sup_t = threading.Thread(target=agent.agent_thread_supervisor, daemon=True)
sup_t.start()
time.sleep(0.25)
agent._asupervise_once = orig_once
agent.ASUPERVISOR_INTERVAL = orig_interval
if sup_t.is_alive():
    ok("O1 supervisor loop survives a raising _asupervise_once")
else:
    bad("O1 supervisor loop survives raising pass")

# P. _asup_log never raises ------------------------------------------------------------------------------
real_dir = agent.SELF_WATCH_DIR
agent.SELF_WATCH_DIR = os.path.join(TMP, "afile")
open(agent.SELF_WATCH_DIR, "w").write("x")  # a file, not a dir
try:
    agent._asup_log("WARN test unwritable")
    ok("P1 _asup_log never raises on unwritable log dir")
except Exception as ex:
    bad("P1 _asup_log never raises", f"raised {ex!r}")
agent.SELF_WATCH_DIR = real_dir

# Q. manager version gate accepts 1.0.10 -------------------------------------------------------------------
try:
    if app._version_at_least("1.0.10", "1.0.2") and not app._version_at_least("1.0.1", "1.0.2"):
        ok("Q1 manager >=1.0.2 gate accepts 1.0.10")
    else:
        bad("Q1 version gate")
except Exception as ex:
    bad("Q1 version gate", f"raised {ex!r}")

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
