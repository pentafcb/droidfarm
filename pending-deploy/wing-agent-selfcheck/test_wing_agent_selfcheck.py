#!/usr/bin/env python3
"""Tests for wing agent 1.0.17 HTTP self-check (main-node df-watchdog v5/v12
parity).

REAL GAP: the main node got a manager self-check in df-watchdog v5 (probe
hardened in v12: a truncated 200 counts as failed) — a wedged-but-alive
manager gets restarted instead of sitting dark. Wing agents had nothing:
a wedged agent HTTP server (thread pool exhausted — the exact failure
http-server-bounds mitigates but cannot repair) is invisible to systemd and
stops heartbeats, screenshots, creates and logins while the process looks
alive. Agent 1.0.17 adds sw_check_agent_http(), run once per selfwatch pass:
it probes its own /api/agent/health (unauthenticated by design), requires a
COMPLETE body (v12 parity), and on 2 consecutive failures logs one ALERT and
exits the process — systemd Restart=always (install-wing.sh) revives it in
~5s. Circuit-broken: >2 self-exits/hour -> agent.http.degraded marker
(reason on first line), alert-only until a healthy probe clears it. Skipped
while the server is not bound yet and when not running under systemd
(exiting there would orphan the node; one WARN per day, never silent).

This suite covers:

  A. agent.VERSION is 1.0.17+ + manager >=1.0.2 gate compatibility
     (exact 1.0.18 bump now owned by wing-create-preflight)
  B. sw_under_systemd: INVOCATION_ID / JOURNAL_STREAM / neither / empty
  C. sw_probe_agent_http (real localhost servers): healthy / truncated 200 /
     500 / empty 200 / connection refused / garbage port
  D. check skipped while _AGENT_HTTP_BOUND is False (no probe, no files)
  E. check skipped on Windows (IS_WINDOWS patched)
  F. non-systemd: skipped + once-per-day WARN (day-file dedup, stale cleanup)
  G. healthy probe: "ok", strikes cleared, degraded cleared + recovered log,
     healthy no-op writes nothing
  H. strike 1: "strike", strikes file, probe-failed log line
  I. strike 2: exit_fn called, "exited", ALERT logged, recoveries bumped,
     strikes file removed
  J. truncated probe: distinct "body truncated" log line
  K. already degraded + failed probe: "degraded", no exit, no repeat DEGRADED
  L. budget exhaustion (3rd exit in an hour): degraded marker written with
     reason on first line, single DEGRADED log, exit_fn NOT called
  M. degraded + healthy probe: marker cleared, recovered log, "ok"
  N. never raises: st_dir is a file, raising probe, garbage port
  O. wiring: selfwatch_loop calls sw_check_agent_http(); main() sets
     _AGENT_HTTP_BOUND after the bind; constants; health endpoint is
     unauthenticated; BOUND_PORT defaulting
  P. degraded-map visibility: agent.http.degraded surfaces via
     selfwatch_state()
"""
import importlib.util
import inspect
import os
import re
import socket
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


agent = load("wing_agent_under_test", AGENT)
app = load("manager_app_under_test", APP)

print("== A. VERSION bump + manager gate ==")
check(agent.VERSION in ("1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"),
      "A1 VERSION is 1.0.17+ (exact bump now owned by wing-auth-ratelimit)",
      repr(agent.VERSION))
check(app._version_at_least("1.0.17", "1.0.2")
      and not app._version_at_least("1.0.1", "1.0.2"),
      "A2 manager >=1.0.2 gate accepts 1.0.17", "gate contract broken")

print("== B. sw_under_systemd ==")
check(agent.sw_under_systemd({"INVOCATION_ID": "abc"}) is True,
      "B1 INVOCATION_ID -> True")
check(agent.sw_under_systemd({"JOURNAL_STREAM": "9:123"}) is True,
      "B2 JOURNAL_STREAM -> True")
check(agent.sw_under_systemd({}) is False, "B3 empty env -> False")
check(agent.sw_under_systemd({"INVOCATION_ID": ""}) is False,
      "B4 empty INVOCATION_ID -> False")
check(agent.sw_under_systemd(None) is True or
      agent.sw_under_systemd(None) is False,
      "B5 None env never raises")
class _RaisingEnv(dict):
    def get(self, k, d=None):
        raise RuntimeError("boom")
check(agent.sw_under_systemd(_RaisingEnv()) is False,
      "B6 raising env -> False, never raises")

print("== C. sw_probe_agent_http (real localhost servers) ==")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _serve_once(port, raw_response):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    srv.settimeout(5)
    def _run():
        try:
            c, _ = srv.accept()
            c.settimeout(5)
            try:
                c.recv(4096)
            except Exception:
                pass
            c.sendall(raw_response)
            time.sleep(0.3)
            c.close()
        except Exception:
            pass
        finally:
            srv.close()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


_body = b'{"ok": true, "version": "1.0.17"}'
p1 = _free_port()
_serve_once(p1, b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
            % (len(_body), _body))
st, tr = agent.sw_probe_agent_http(p1)
check((st, tr) == ("ok", False), "C1 healthy 200+JSON -> ok", repr((st, tr)))

p2 = _free_port()
# truncated: headers promise 100 bytes, connection dies after 10
_serve_once(p2, b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n"
            + b"0123456789")
st, tr = agent.sw_probe_agent_http(p2)
check((st, tr) == ("bad", True), "C2 truncated 200 -> bad+truncated",
      repr((st, tr)))

p3 = _free_port()
_serve_once(p3, b"HTTP/1.1 500 Bang\r\nContent-Length: 5\r\nConnection: close\r\n\r\nerror")
st, tr = agent.sw_probe_agent_http(p3)
check((st, tr) == ("bad", False), "C3 500 -> bad, not truncated",
      repr((st, tr)))

p4 = _free_port()
_serve_once(p4, b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
st, tr = agent.sw_probe_agent_http(p4)
check((st, tr) == ("bad", True), "C4 empty 200 -> bad+truncated",
      repr((st, tr)))

st, tr = agent.sw_probe_agent_http(_free_port())  # nothing listening
check((st, tr) == ("bad", False), "C5 refused -> bad, not truncated",
      repr((st, tr)))
st, tr = agent.sw_probe_agent_http("bogus")
check((st, tr) == ("bad", False), "C6 garbage port -> bad, never raises",
      repr((st, tr)))

print("== D..N. sw_check_agent_http state machine ==")

_tmp = tempfile.mkdtemp(prefix="swselfcheck")


def _newdir():
    return tempfile.mkdtemp(prefix="swselfcheck", dir=_tmp)


_SYS_ENV = {"INVOCATION_ID": "test"}
_NO_ENV = {}


def _log(d):
    p = os.path.join(d, "selfwatch.log")
    try:
        return open(p).read()
    except Exception:
        return ""


class _Exiter:
    def __init__(self):
        self.calls = 0
    def __call__(self):
        self.calls += 1


# module-state patching (restored at the end)
_old_bound = agent._AGENT_HTTP_BOUND
_old_win = agent.IS_WINDOWS
_old_port = agent.BOUND_PORT
agent._AGENT_HTTP_BOUND = True
agent.IS_WINDOWS = False

try:
    print("== D. not bound yet ==")
    d = _newdir()
    agent._AGENT_HTTP_BOUND = False
    def _boom_probe(port):
        raise AssertionError("probe must not run before bind")
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=_boom_probe,
                                  exit_fn=_Exiter())
    check(r == "skipped", "D1 unbound -> skipped", repr(r))
    check(os.listdir(d) == [], "D2 unbound writes no files",
          repr(os.listdir(d)))
    agent._AGENT_HTTP_BOUND = True

    print("== E. Windows skip ==")
    d = _newdir()
    agent.IS_WINDOWS = True
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=_boom_probe, exit_fn=_Exiter())
    check(r == "skipped", "E1 Windows -> skipped", repr(r))
    agent.IS_WINDOWS = False

    print("== F. non-systemd: skipped + once-per-day WARN ==")
    d = _newdir()
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_NO_ENV,
                                  probe=_boom_probe, exit_fn=_Exiter())
    check(r == "skipped", "F1 non-systemd -> skipped", repr(r))
    lg = _log(d)
    check("self-check disabled" in lg and "systemd" in lg,
          "F2 one WARN line, never silent", repr(lg[-120:]))
    dayfiles = [f for f in os.listdir(d)
                if f.startswith("agent.http.nosystemd.")]
    check(len(dayfiles) == 1, "F3 day-file created", repr(dayfiles))
    before = _log(d)
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_NO_ENV,
                                  probe=_boom_probe, exit_fn=_Exiter())
    check(_log(d) == before, "F4 second call same day: no repeat WARN")
    # stale day-file cleanup
    stale = os.path.join(d, "agent.http.nosystemd.2000-01-01")
    open(stale, "w").close()
    os.remove(os.path.join(d, dayfiles[0]))
    agent.sw_check_agent_http(port=1, st_dir=d, env=_NO_ENV,
                              probe=_boom_probe, exit_fn=_Exiter())
    check(not os.path.exists(stale), "F5 stale day-file removed")
    check("systemd" in _log(d), "F6 WARN re-logged on new day")

    print("== G. healthy probe ==")
    d = _newdir()
    ex = _Exiter()
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=lambda p: ("ok", False), exit_fn=ex)
    check(r == "ok", "G1 healthy -> ok", repr(r))
    check(ex.calls == 0, "G2 healthy never exits")
    check(not os.path.exists(os.path.join(d, "agent.http.strikes")),
          "G3 no strikes file on healthy")
    before = _log(d)
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=lambda p: ("ok", False), exit_fn=ex)
    check(r == "ok" and _log(d) == before, "G4 healthy no-op: no log spam")
    # degraded + healthy -> clears marker + recovered log
    open(os.path.join(d, "agent.http.degraded"), "w").write("x\n")
    open(os.path.join(d, "agent.http.exit.ev"), "w").write("1\n")
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=lambda p: ("ok", False), exit_fn=ex)
    check(r == "ok", "G5 degraded+healthy -> ok", repr(r))
    check(not os.path.exists(os.path.join(d, "agent.http.degraded")),
          "G6 degraded marker cleared")
    check(not os.path.exists(os.path.join(d, "agent.http.exit.ev")),
          "G7 exit event file cleared")
    check("recovered from DEGRADED" in _log(d), "G8 recovered log line")

    print("== H. strike 1 ==")
    d = _newdir()
    ex = _Exiter()
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=lambda p: ("bad", False), exit_fn=ex)
    check(r == "strike", "H1 first failure -> strike", repr(r))
    sf = os.path.join(d, "agent.http.strikes")
    try:
        n = open(sf).read().strip()
    except Exception:
        n = "?"
    check(n == "1", "H2 strikes file == 1", repr(n))
    check("probe failed" in _log(d) and "strike 1/2" in _log(d),
          "H3 probe-failed log line with strike count")
    check(ex.calls == 0, "H4 no exit on first strike")

    print("== I. strike 2 -> exit ==")
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=lambda p: ("bad", False), exit_fn=ex)
    check(r == "exited", "I1 second failure -> exited", repr(r))
    check(ex.calls == 1, "I2 exit_fn called exactly once")
    lg = _log(d)
    check("ALERT" in lg and "exiting for systemd restart" in lg,
          "I3 ALERT log line names the systemd restart")
    check("exit 1/2 this hour" in lg, "I4 exit budget in log line")
    try:
        rec = int(__import__("json").load(
            open(os.path.join(d, "recoveries.json")))["total"])
    except Exception:
        rec = -1
    check(rec == 1, "I5 recoveries.json bumped", repr(rec))
    check(not os.path.exists(sf), "I6 strikes file removed after exit")

    print("== J. truncated probe ==")
    d = _newdir()
    ex = _Exiter()
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=lambda p: ("bad", True), exit_fn=ex)
    check(r == "strike", "J1 truncated -> strike (not ok)", repr(r))
    check("body truncated" in _log(d), "J2 distinct truncated log line")

    print("== K. already degraded + failed probe ==")
    d = _newdir()
    ex = _Exiter()
    open(os.path.join(d, "agent.http.degraded"), "w").write("old reason\n")
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=lambda p: ("bad", False), exit_fn=ex)
    check(r == "degraded", "K1 degraded + failed -> degraded", repr(r))
    check(ex.calls == 0, "K2 no exit while degraded (alert-only)")
    lg = _log(d)
    check(lg.count("DEGRADED") == 0 or "recovered" in lg,
          "K3 no repeat DEGRADED line while degraded", repr(lg[-100:]))

    print("== L. budget exhaustion: 3rd exit in an hour ==")
    d = _newdir()
    ex = _Exiter()
    now = int(time.time())
    with open(os.path.join(d, "agent.http.exit.ev"), "w") as fh:
        fh.write("%d\n%d\n" % (now - 100, now - 50))
    # drive two consecutive failures: strikes hit 2 -> cb_guard trips
    agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                              probe=lambda p: ("bad", False), exit_fn=ex)
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=lambda p: ("bad", False), exit_fn=ex)
    check(r == "degraded", "L1 budget exhausted -> degraded", repr(r))
    check(ex.calls == 0, "L2 exit_fn NOT called on budget trip")
    mf = os.path.join(d, "agent.http.degraded")
    check(os.path.exists(mf), "L3 degraded marker written")
    try:
        first = open(mf).readline().strip()
    except Exception:
        first = ""
    check("agent HTTP server" in first and "auto-recovery paused" in first,
          "L4 reason on marker first line", repr(first))
    lg = _log(d)
    check(lg.count("DEGRADED: agent HTTP server") == 1,
          "L5 exactly one DEGRADED log line", repr(lg[-160:]))
    before = _log(d)
    lg_before = before.count("DEGRADED: agent HTTP server")
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=lambda p: ("bad", False), exit_fn=ex)
    check(r == "degraded" and ex.calls == 0
          and _log(d).count("DEGRADED: agent HTTP server") == lg_before,
          "L6 repeat while degraded: no exit, no new DEGRADED line")

    print("== M. degraded + healthy probe ==")
    d = _newdir()
    ex = _Exiter()
    open(os.path.join(d, "agent.http.degraded"), "w").write("x\n")
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=lambda p: ("ok", False), exit_fn=ex)
    check(r == "ok", "M1 degraded+healthy -> ok", repr(r))
    check(not os.path.exists(os.path.join(d, "agent.http.degraded")),
          "M2 marker cleared on recovery")
    check("recovered from DEGRADED" in _log(d), "M3 recovered log line")
    check(ex.calls == 0, "M4 no exit on recovery")

    print("== N. never raises ==")
    fpath = os.path.join(_tmp, "notadir")
    open(fpath, "w").close()
    r = agent.sw_check_agent_http(port=1, st_dir=fpath, env=_SYS_ENV,
                                  probe=lambda p: ("bad", False),
                                  exit_fn=_Exiter())
    check(r in ("strike", "skipped"), "N1 st_dir is a file -> no raise",
          repr(r))
    def _raising_probe(p):
        raise RuntimeError("boom")
    d = _newdir()
    r = agent.sw_check_agent_http(port=1, st_dir=d, env=_SYS_ENV,
                                  probe=_raising_probe, exit_fn=_Exiter())
    check(r == "skipped", "N2 raising probe -> skipped, never raises",
          repr(r))
    r = agent.sw_probe_agent_http(None)
    check(r == ("bad", False), "N3 probe(None port) -> bad, never raises")
    check(agent.sw_check_agent_http(port=1, st_dir=d, env="bogus",
                                    probe=lambda p: ("ok", False),
                                    exit_fn=_Exiter()) == "skipped",
          "N4 garbage env -> skipped (cannot confirm systemd), never raises")

    print("== O. wiring ==")
    src_pass = inspect.getsource(agent.selfwatch_pass_once)
    check("sw_check_agent_http()" in src_pass,
          "O1 selfwatch_pass_once calls sw_check_agent_http() (1.0.23 lock: "
          "per-process self-check runs regardless of the lock)")
    src_main = inspect.getsource(agent.main)
    check("_AGENT_HTTP_BOUND = True" in src_main,
          "O2 main() sets _AGENT_HTTP_BOUND after the bind")
    src_check = inspect.getsource(agent.sw_check_agent_http)
    check("if not _AGENT_HTTP_BOUND" in src_check,
          "O3 check honors the bound flag (startup race)")
    check("if port is None" in src_check and "port = BOUND_PORT" in src_check,
          "O4 port defaults to BOUND_PORT at call time")
    check(agent.SW_AGENT_HTTP_TIMEOUT == 10,
          "O5 probe timeout 10s (main v5 parity)")
    check(agent.SW_AGENT_HTTP_STRIKES == 2,
          "O6 2-strike exit (main v5 parity)")
    check(agent.SW_AGENT_HTTP_MAX_EXITS == 2
          and agent.SW_AGENT_HTTP_WINDOW == 3600,
          "O7 >2 exits/hour -> degraded (main v5 parity)")
    src_handler = inspect.getsource(agent.Handler._authed)
    check('if self.path == "/api/agent/health"' in src_handler,
          "O8 /api/agent/health is unauthenticated (self-probe needs no token)")
    check("def exit_fn():" in src_check and "os._exit(1)" in src_check,
          "O9 default exit_fn is os._exit(1)")
    check("agent.http.degraded" in src_check
          and "agent.http.strikes" in src_check,
          "O10 state-file names")

    print("== P. degraded-map visibility ==")
    d = _newdir()
    open(os.path.join(d, "agent.http.degraded"), "w").write("reason here\n")
    st = agent.selfwatch_state(st_dir=d)
    check(st["degraded"].get("agent.http") == "reason here",
          "P1 agent.http.degraded in selfwatch_state degraded map",
          repr(st["degraded"]))

finally:
    agent._AGENT_HTTP_BOUND = _old_bound
    agent.IS_WINDOWS = _old_win
    agent.BOUND_PORT = _old_port

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
