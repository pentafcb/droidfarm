#!/usr/bin/env python3
"""Tests for the manager background-thread supervisor.

Covers:
  A. registration contract
  B. first pass starts registered threads
  C. healthy pass is a no-op (no restart spam, no log spam)
  D. dead thread gets restarted
  E. circuit breaker: >3 restarts/hour -> give up + one ALERT, no factory calls
  F. sliding window expiry re-arms the budget
  G. raising factory is caught, logged, retried next pass
  H. _sup_log writes to DF_WATCHDOG_LOG, never raises
  I. corrupt registry entries never break a pass
  J. wiring contract: main() registers both watchers + starts the supervisor,
     old direct .start() lines are gone
  K. constants sane
  L. thread_supervisor loop calls _supervise_once repeatedly and survives it raising
"""
import importlib.util
import inspect
import os
import re
import sys
import tempfile
import threading
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
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


def check(name, cond, detail=""):
    if cond:
        ok(name)
    else:
        bad(name, detail)


LOGFILE = tempfile.NamedTemporaryFile(delete=False, suffix=".log").name
os.environ["DF_WATCHDOG_LOG"] = LOGFILE


def load_app():
    spec = importlib.util.spec_from_file_location("df_app_sup", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = load_app()


def log_lines():
    with open(LOGFILE) as f:
        return f.read().splitlines()


def fresh():
    mod._SUPERVISED.clear()
    with open(LOGFILE, "w"):
        pass


def mkthread(target=lambda: None, name="t"):
    return threading.Thread(target=target, daemon=True, name=name)


# --- A. registration ---------------------------------------------------------
fresh()
mod.register_supervised("w", lambda: mkthread(name="w"))
mod.register_supervised("p", lambda: mkthread(name="p"))
check("A1 both entries registered", set(mod._SUPERVISED) == {"w", "p"})
check("A2 initial thread is None",
      all(e["thread"] is None for e in mod._SUPERVISED.values()))
check("A3 initial given_up False",
      all(e["given_up"] is False for e in mod._SUPERVISED.values()))

# --- B. first pass starts threads -------------------------------------------
fresh()
calls = {"w": 0, "p": 0}


def fw():
    calls["w"] += 1
    return mkthread(target=lambda: time.sleep(30), name="w")


def fp():
    calls["p"] += 1
    return mkthread(target=lambda: time.sleep(30), name="p")


mod.register_supervised("w", fw)
mod.register_supervised("p", fp)
mod._supervise_once()
check("B1 watcher thread alive", mod._SUPERVISED["w"]["thread"].is_alive())
check("B2 poller thread alive", mod._SUPERVISED["p"]["thread"].is_alive())
check("B3 thread names kept",
      mod._SUPERVISED["w"]["thread"].name == "w"
      and mod._SUPERVISED["p"]["thread"].name == "p")
check("B4 factory called exactly once each", calls == {"w": 1, "p": 1})

# --- C. healthy pass is a no-op ---------------------------------------------
n_before = len(log_lines())
mod._supervise_once()
check("C1 no additional factory calls on healthy pass", calls == {"w": 1, "p": 1})
check("C2 no new log lines on healthy pass", len(log_lines()) == n_before)

# --- D. dead thread gets restarted ------------------------------------------
fresh()
dcalls = {"n": 0}


def fdead():
    dcalls["n"] += 1
    return mkthread(target=lambda: None, name="dead")  # exits immediately


mod.register_supervised("dead", fdead)
mod._supervise_once()
time.sleep(0.3)  # let the first thread exit
check("D0 first thread actually died",
      not mod._SUPERVISED["dead"]["thread"].is_alive())
mod._supervise_once()
check("D1 factory called again after death", dcalls["n"] == 2)
check("D2 restart logged with WARN + name",
      any("WARN supervisor started background thread 'dead'" in ln
          for ln in log_lines()))

# --- E. circuit breaker ------------------------------------------------------
fresh()
ecalls = {"n": 0}
mod.register_supervised("storm", lambda: (ecalls.__setitem__("n", ecalls["n"] + 1),
                                          mkthread(name="storm"))[1])
now = time.time()
mod._SUPERVISED["storm"]["restarts"] = [now, now - 10, now - 20]
mod._supervise_once()
check("E1 given_up after budget trip", mod._SUPERVISED["storm"]["given_up"] is True)
check("E2 ALERT line in log",
      any("ALERT supervisor gave up restarting 'storm'" in ln for ln in log_lines()))
check("E3 factory not called on trip", ecalls["n"] == 0)
n_alert = sum("gave up restarting 'storm'" in ln for ln in log_lines())
mod._supervise_once()
mod._supervise_once()
check("E4 no duplicate ALERT on repeat passes",
      sum("gave up restarting 'storm'" in ln for ln in log_lines()) == n_alert == 1)

# --- F. sliding window expiry ------------------------------------------------
fresh()
fcalls = {"n": 0}


def ffresh():
    fcalls["n"] += 1
    return mkthread(target=lambda: time.sleep(30), name="fresh")


mod.register_supervised("fresh", ffresh)
old = time.time() - 3700
mod._SUPERVISED["fresh"]["restarts"] = [old, old - 5, old - 9]
mod._supervise_once()
check("F1 old restarts pruned, restart happens",
      fcalls["n"] == 1 and mod._SUPERVISED["fresh"]["thread"].is_alive())
check("F2 given_up stays False", mod._SUPERVISED["fresh"]["given_up"] is False)

# --- G. raising factory ------------------------------------------------------
fresh()
gcalls = {"n": 0}


def gfail():
    gcalls["n"] += 1
    m = mock.Mock()
    m.start.side_effect = RuntimeError("boom")
    return m


mod.register_supervised("badf", gfail)
mod._supervise_once()
check("G1 raising factory: no raise, thread stays None",
      mod._SUPERVISED["badf"]["thread"] is None and gcalls["n"] == 1)
check("G2 WARN logged with the error",
      any("WARN supervisor failed to (re)start 'badf': boom" in ln
          for ln in log_lines()))
mod._supervise_once()
check("G3 next pass retries the factory", gcalls["n"] == 2)

# --- H. _sup_log -------------------------------------------------------------
fresh()
mod._sup_log("WARN supervisor test line")
lines = log_lines()
check("H1 timestamped line written to DF_WATCHDOG_LOG",
      len(lines) == 1 and re.match(
          r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} WARN supervisor test line$", lines[0]),
      lines[-1] if lines else "empty")
saved_log = mod.WATCHDOG_LOG
mod.WATCHDOG_LOG = tempfile.gettempdir()  # a directory: open() raises
try:
    mod._sup_log("WARN x")
    raised = False
except Exception:
    raised = True
finally:
    mod.WATCHDOG_LOG = saved_log
check("H2 unwritable log path never raises", not raised)
mod._sup_log("ALERT supervisor test alert")
check("H3 ALERT wording carries the human-attention note",
      any("ALERT supervisor test alert" in ln for ln in log_lines()))

# --- I. corrupt registry -----------------------------------------------------
fresh()
mod._SUPERVISED["nofactory"] = {"thread": None, "restarts": [], "given_up": False}
mod._SUPERVISED["badrestarts"] = {"factory": lambda: mkthread(name="ok"),
                                  "thread": None, "restarts": [None], "given_up": False}
try:
    mod._supervise_once()
    raised = False
except Exception:
    raised = True
check("I1 entry without factory skipped, pass survives", not raised)
check("I2 corrupt restarts list does not poison the pass", not raised)

# --- J. wiring contract ------------------------------------------------------
src = inspect.getsource(mod.main)
check("J1 main registers auto-login-watcher",
      'register_supervised(\n        "auto-login-watcher"' in src or
      'register_supervised("auto-login-watcher"' in src)
check("J2 main registers resource-poller",
      '"resource-poller"' in src and "register_supervised" in src)
check("J3 main starts thread_supervisor",
      "target=thread_supervisor" in src)
check("J4 old direct .start() lines for the watchers are gone",
      'name="auto-login-watcher").start()' not in src
      and 'name="resource-poller").start()' not in src)

# --- K. constants ------------------------------------------------------------
check("K1 SUPERVISOR_INTERVAL == 30", mod.SUPERVISOR_INTERVAL == 30)
check("K2 SUPERVISOR_MAX_RESTARTS == 3", mod.SUPERVISOR_MAX_RESTARTS == 3)
check("K3 SUPERVISOR_WINDOW == 3600", mod.SUPERVISOR_WINDOW == 3600)

# --- L. supervisor loop ------------------------------------------------------
fresh()
orig_interval = mod.SUPERVISOR_INTERVAL
orig_once = mod._supervise_once
lcalls = {"n": 0, "raise_once": True}


def fake_once():
    lcalls["n"] += 1
    if lcalls["raise_once"]:
        lcalls["raise_once"] = False
        raise RuntimeError("boom")


mod.SUPERVISOR_INTERVAL = 0.01
mod._supervise_once = fake_once
t = threading.Thread(target=mod.thread_supervisor, daemon=True)
t.start()
time.sleep(0.15)
n1 = lcalls["n"]
check("L1 loop calls _supervise_once repeatedly", n1 >= 3, f"calls={n1}")
time.sleep(0.15)
n2 = lcalls["n"]
check("L2 loop survives a raising _supervise_once", n2 > n1, f"{n1}->{n2}")
check("L3 loop keeps calling after the raise", n2 >= n1 + 2, f"{n1}->{n2}")
mod.SUPERVISOR_INTERVAL = orig_interval
mod._supervise_once = orig_once

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
