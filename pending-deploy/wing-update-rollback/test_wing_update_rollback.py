"""Unit tests for wing agent 1.0.30 self-update rollback.

Gap: maybe_agent_self_update() (agent 1.0.24) swaps in a sha256-verified,
py_compile-clean agent.py and exits. py_compile catches syntax errors, but a
runtime bug (a module-level NameError, a bad import, a crash inside main())
slips through and crash-loops — under systemd's Restart=always AND the
__main__ while-True loop — with no recovery. The agent IS the node's recovery
mechanism, so a crash-looping agent is a dead node until a human intervenes:
exactly what self-update was built to avoid.

Fix: a successful update leaves a dated marker (note_agent_update); every
startup records a boot timestamp (record_agent_boot); a boot storm
(>= ROLLBACK_BOOTS startups inside ROLLBACK_WINDOW) while the marker is fresh
(<= ROLLBACK_MARKER_TTL after the update) restores agent.py.bak — the code
that was running minutes ago — and re-execs it (maybe_rollback_agent_update,
called at the top of main()). The marker is single-use, so a broken
environment can never rollback-loop.

Covers: VERSION exactly 1.0.30 + manager >=1.0.2 gate, note_agent_update
(marker JSON shape, atomic no-.tmp-left, bootlog reset, never raises),
record_agent_boot (append, prune-old, garbage lines, never raises),
_read_update_marker (missing/corrupt/expired/wrong-type -> None, fresh ->
dict), maybe_rollback_agent_update (no-marker records boot, ok below
threshold, storm -> rolled-back with .bak restored + marker cleared + ALERT
+ execv argv, storm with missing .bak -> no-backup, storm with
uncompilable .bak -> no-backup, marker cleared on every storm outcome so no
rollback loop, garbage st_dir never raises), maybe_agent_self_update writes
the marker on success (fake fetch/apply/exit), main() wiring contract (call
at top of main, before thread registration).

Run: python3 test_wing_update_rollback.py

Hermetic: the agent and manager modules are loaded from the repo tree; all
file writes go to tmp dirs; os._exit is never reached in tests (fake execv).
No docker, no network, no systemd.
"""
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("wur_agent", AGENT)
app = load("wur_app", APP)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name, detail)


def mkroot():
    return tempfile.mkdtemp(prefix="wur_")


# ---------- A. version ----------------------------------------------------
check("A1 VERSION is 1.0.30+ (1.0.31 adds instance resize)",
      agent.VERSION in ("1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"),
      repr(agent.VERSION))
check("A2 parse_agent_version ordering: 1.0.30 > 1.0.29",
      agent.parse_agent_version("1.0.30") > agent.parse_agent_version("1.0.29"))
check("A3 manager >=1.0.2 gate accepts 1.0.30",
      app._version_at_least("1.0.30", "1.0.2")
      and not app._version_at_least("1.0.1", "1.0.2"))
check("A4 constants sane",
      agent.ROLLBACK_BOOTS == 4 and agent.ROLLBACK_WINDOW == 300
      and agent.ROLLBACK_MARKER_TTL == 1800
      and agent.UPDATE_MARKER == "agent.update.marker"
      and agent.UPDATE_BOOTLOG == "agent.boot.log")

# ---------- B. note_agent_update ------------------------------------------
st = mkroot()
check("B1 returns True", agent.note_agent_update(st, "1.0.30", "1.0.29") is True)
mp = os.path.join(st, "agent.update.marker")
check("B2 marker file exists", os.path.isfile(mp))
with open(mp) as f:
    m = json.load(f)
check("B3 marker JSON shape",
      m["version"] == "1.0.30" and m["from"] == "1.0.29"
      and isinstance(m["ts"], float) and m["ts"] <= time.time() < m["ts"] + 60,
      repr(m))
check("B4 atomic: no .tmp left", not os.path.exists(mp + ".tmp"))
check("B5 bootlog reset", not os.path.exists(os.path.join(st, "agent.boot.log")))
check("B6 garbage st_dir never raises",
      agent.note_agent_update(os.path.join(st, "..", "..", "no-such-dir",
                                           "\x00bad"), "1.0.30", "1.0.29")
      in (True, False))

# ---------- C. record_agent_boot ------------------------------------------
st = mkroot()
b1 = agent.record_agent_boot(st)
check("C1 first boot recorded", len(b1) == 1 and b1[0] <= time.time())
b2 = agent.record_agent_boot(st)
check("C2 second boot appended", len(b2) == 2)
# old entries pruned
old = time.time() - agent.ROLLBACK_WINDOW - 60
with open(os.path.join(st, "agent.boot.log"), "w") as f:
    f.write("%.3f\n%.3f\ngarbage\n\n" % (old, time.time()))
b3 = agent.record_agent_boot(st)
check("C3 old + garbage pruned", len(b3) == 2, repr(b3))
# future timestamps rejected (clock skew can never pin the storm count)
with open(os.path.join(st, "agent.boot.log"), "a") as f:
    f.write("%.3f\n" % (time.time() + 99999))
b4 = agent.record_agent_boot(st)
check("C4 future ts dropped", len(b4) == 3, repr(b4))
check("C5 garbage st_dir never raises",
      isinstance(agent.record_agent_boot("/proc/nope/\x00"), list))

# ---------- D. _read_update_marker ----------------------------------------
st = mkroot()
check("D1 missing -> None", agent._read_update_marker(st) is None)
with open(os.path.join(st, "agent.update.marker"), "w") as f:
    f.write("not json{")
check("D2 corrupt -> None", agent._read_update_marker(st) is None)
with open(os.path.join(st, "agent.update.marker"), "w") as f:
    json.dump([1, 2, 3], f)
check("D3 non-dict -> None", agent._read_update_marker(st) is None)
with open(os.path.join(st, "agent.update.marker"), "w") as f:
    json.dump({"version": "1.0.30", "from": "1.0.29",
               "ts": time.time() - agent.ROLLBACK_MARKER_TTL - 10}, f)
check("D4 expired -> None", agent._read_update_marker(st) is None)
agent.note_agent_update(st, "1.0.30", "1.0.29")
m = agent._read_update_marker(st)
check("D5 fresh -> dict", isinstance(m, dict) and m["version"] == "1.0.30",
      repr(m))

# ---------- E. maybe_rollback_agent_update --------------------------------
# E1: no marker -> records a boot, returns no-marker, touches nothing else
st = mkroot()
ap = os.path.join(st, "agent.py")
with open(ap, "w") as f:
    f.write("NEW = 1\n")
calls = []
r = agent.maybe_rollback_agent_update(_st_dir=st, _agent_path=ap,
                                      _execv=lambda e, a: calls.append((e, a)))
check("E1 no-marker", r == "no-marker", repr(r))
check("E1b boot recorded", len(agent.record_agent_boot(st)) == 2)
check("E1c agent.py untouched", open(ap).read() == "NEW = 1\n")
check("E1d no execv", calls == [])

# E2: marker but below threshold -> ok, file untouched
st = mkroot()
ap = os.path.join(st, "agent.py")
with open(ap, "w") as f:
    f.write("NEW = 1\n")
agent.note_agent_update(st, "1.0.30", "1.0.29")
agent.record_agent_boot(st)  # 1 boot, need 4
r = agent.maybe_rollback_agent_update(_st_dir=st, _agent_path=ap,
                                      _execv=lambda e, a: calls.append((e, a)))
check("E2 ok below threshold", r == "ok", repr(r))
check("E2b marker kept", os.path.isfile(os.path.join(st, "agent.update.marker")))
check("E2c agent.py untouched", open(ap).read() == "NEW = 1\n")

# E3: storm -> rolled-back: .bak restored, marker+bootlog cleared, ALERT, execv
st = mkroot()
ap = os.path.join(st, "agent.py")
with open(ap, "w") as f:
    f.write("NEWCODE syntax is fine but crashes at runtime\n")
with open(ap + ".bak", "w") as f:
    f.write("OLDCODE = 1\n")
agent.note_agent_update(st, "1.0.30", "1.0.29")
for _ in range(3):
    agent.record_agent_boot(st)
calls = []
r = agent.maybe_rollback_agent_update(_st_dir=st, _agent_path=ap,
                                      _execv=lambda e, a: calls.append((e, a)))
check("E3 rolled-back", r == "rolled-back", repr(r))
check("E3b .bak restored", open(ap).read() == "OLDCODE = 1\n")
check("E3c marker cleared", not os.path.exists(os.path.join(st, "agent.update.marker")))
check("E3d bootlog cleared", not os.path.exists(os.path.join(st, "agent.boot.log")))
check("E3e execv argv", len(calls) == 1 and calls[0][0] == sys.executable
      and calls[0][1][1] == ap, repr(calls))
logp = os.path.join(st, "selfwatch.log")
log = open(logp).read() if os.path.isfile(logp) else ""
check("E3f ALERT logged", "ALERT agent rolled back 1.0.30 -> 1.0.29" in log, log[-200:])
# after rollback the next boot is clean (no rollback loop)
r2 = agent.maybe_rollback_agent_update(_st_dir=st, _agent_path=ap,
                                       _execv=lambda e, a: calls.append((e, a)))
check("E3g next boot no-marker", r2 == "no-marker", repr(r2))
check("E3h no second execv", len(calls) == 1)

# E4: storm but .bak missing -> no-backup, marker cleared (no loop), no execv
st = mkroot()
ap = os.path.join(st, "agent.py")
with open(ap, "w") as f:
    f.write("NEW = 1\n")
agent.note_agent_update(st, "1.0.30", "1.0.29")
for _ in range(3):
    agent.record_agent_boot(st)
calls = []
r = agent.maybe_rollback_agent_update(_st_dir=st, _agent_path=ap,
                                      _execv=lambda e, a: calls.append((e, a)))
check("E4 no-backup", r == "no-backup", repr(r))
check("E4b marker cleared", not os.path.exists(os.path.join(st, "agent.update.marker")))
check("E4c no execv", calls == [])
check("E4d agent.py untouched", open(ap).read() == "NEW = 1\n")
logp = os.path.join(st, "selfwatch.log")
log = open(logp).read() if os.path.isfile(logp) else ""
check("E4e skip ALERT logged", "rollback skipped" in log, log[-200:])

# E5: storm but .bak is garbage -> no-backup (fail-closed, never swap junk)
st = mkroot()
ap = os.path.join(st, "agent.py")
with open(ap, "w") as f:
    f.write("NEW = 1\n")
with open(ap + ".bak", "w") as f:
    f.write("def broken(:\n")
agent.note_agent_update(st, "1.0.30", "1.0.29")
for _ in range(3):
    agent.record_agent_boot(st)
calls = []
r = agent.maybe_rollback_agent_update(_st_dir=st, _agent_path=ap,
                                      _execv=lambda e, a: calls.append((e, a)))
check("E5 garbage .bak -> no-backup", r == "no-backup", repr(r))
check("E5b agent.py untouched", open(ap).read() == "NEW = 1\n")
check("E5c no execv", calls == [])

# E6: garbage st_dir never raises
r = agent.maybe_rollback_agent_update(_st_dir="/proc/nope/\x00",
                                      _agent_path="/proc/nope/\x00")
check("E6 garbage never raises", r in ("no-marker", "ok", "error", "no-backup"),
      repr(r))

# E7: storm counted across the __main__ retry cadence (10s sleeps + 5s
# systemd restarts both land inside the 300s window) — simulated by
# driving the real function 4x with a live marker
st = mkroot()
ap = os.path.join(st, "agent.py")
with open(ap, "w") as f:
    f.write("NEW = 1\n")
with open(ap + ".bak", "w") as f:
    f.write("OLD = 1\n")
agent.note_agent_update(st, "1.0.30", "1.0.29")
calls = []
results = [agent.maybe_rollback_agent_update(_st_dir=st, _agent_path=ap,
                                             _execv=lambda e, a: calls.append((e, a)))
           for _ in range(4)]
check("E7 storm trips on 4th boot", results == ["ok", "ok", "ok", "rolled-back"],
      repr(results))

# ---------- F. update path writes the marker -------------------------------
# Version-relative: the directive must name a version strictly newer than
# the running agent, whatever that is (bump-proof across VERSION bumps).
_vparts = list(agent.parse_agent_version(agent.VERSION))
_vparts[-1] += 1
NEXT_VER = ".".join(str(x) for x in _vparts)
st = mkroot()
agent._UPDATE_ATTEMPTED = False
directive = {"version": NEXT_VER, "sha256": "0" * 64}
exits = []
fetches = []
applies = []


def fake_fetch(d):
    fetches.append(d)
    return (True, os.path.join(st, "dl.py"))


def fake_apply(p):
    applies.append(p)
    return True


with open(os.path.join(st, "dl.py"), "w") as f:
    f.write("x = 1\n")
r = agent.maybe_agent_self_update(directive, _env={"INVOCATION_ID": "x"},
                                  _exit_fn=lambda c: exits.append(c),
                                  _fetch=fake_fetch, _apply=fake_apply,
                                  _st_dir=st)
agent._UPDATE_ATTEMPTED = False
check("F1 update succeeds", r == "updated", repr(r))
check("F2 exit called once", exits == [1], repr(exits))
mp = os.path.join(st, "agent.update.marker")
check("F3 marker written on update", os.path.isfile(mp))
with open(mp) as f:
    m = json.load(f)
check("F4 marker names versions", m["version"] == NEXT_VER
      and m["from"] == agent.VERSION, repr(m))
check("F5 update still refuses downgrade (regression)",
      agent.maybe_agent_self_update({"version": "1.0.29", "sha256": "0" * 64},
                                    _st_dir=st) == "current")
agent._UPDATE_ATTEMPTED = False

# ---------- G. main() wiring -------------------------------------------------
src = inspect.getsource(agent.main)
i_rb = src.find("maybe_rollback_agent_update()")
i_sup = src.find("register_supervised")
check("G1 rollback called in main()", i_rb != -1)
check("G2 rollback runs BEFORE thread registration", 0 <= i_rb < i_sup,
      "rb=%d sup=%d" % (i_rb, i_sup))
src_up = inspect.getsource(agent.maybe_agent_self_update)
check("G3 update path records the marker",
      "note_agent_update(st_dir, directive.get(\"version\"), VERSION)" in src_up)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
