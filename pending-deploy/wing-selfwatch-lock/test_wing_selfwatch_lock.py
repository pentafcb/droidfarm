"""Unit tests for the wing selfwatch single-instance lock (wing agent 1.0.23).

Gap: the agent binds 8090-8099 with a port-conflict fallback, so a stale or
duplicate agent process on the same node (crashed installer re-run, manual
start, the __main__ restart loop racing systemd) binds the next free port and
BOTH processes run the full selfwatch recovery loop — duplicate container
starts, double helper restarts, double disk prunes — while heartbeating the
same provider ID on different ports. The main-node df-watchdog.sh v11 fixed
the same class with a mkdir mutex; this is the thirteenth "main had it,
wing didn't" parity item (1.0.5 drift, 1.0.6 adaptive-screencap, 1.0.7
disk/log, 1.0.11 session-death, 1.0.12 loop devices, 1.0.13 memory, 1.0.14
stuck-boot, 1.0.15 keepalive mirror, 1.0.16 binderfs self-heal, 1.0.17 agent
HTTP self-check, 1.0.18 create image preflight, 1.0.19 auth rate-limit,
1.0.20 status cache, 1.0.21 headroom probe, 1.0.22 per-instance degraded).

Covers: VERSION exact 1.0.23 + manager >=1.0.2 gate, _sw_pid_alive units,
sw_selfwatch_lock units (fresh/re-entrant/foreign/dead-holder/stale-holder/
missing-pidfile/broken-dir/garbage/lockfile-not-a-dir), the once-only note
helpers, selfwatch_pass_once gating (recovery only when held, HTTP
self-check always runs, raising/garbage lock_fn never propagates), the
selfwatch_loop wiring contract, and a thread-churn smoke test.

Run: python3 test_wing_selfwatch_lock.py

Hermetic: fake st_dirs everywhere; the real lock functions run against tmp
dirs with crafted lock state. The agent module is loaded from the repo tree.
"""
import importlib.util
import inspect
import os
import subprocess
import sys
import tempfile
import threading
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


agent = load("wsl_agent", AGENT)
app = load("wsl_app", APP)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")


def fresh_dir(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    return d


def log_lines(d):
    try:
        with open(os.path.join(d, "selfwatch.log")) as fh:
            return fh.read().splitlines()
    except Exception:
        return []


# ---- A. version ----
print("== A. version ==")
# 1.0.25+: this suite owned the pidfile-grace race fix at 1.0.25; the
# exact-bump assertion widens to 1.0.25+ now that wing-route-name-gate owns
# exact 1.0.26 (handoff pattern).
check("agent VERSION is 1.0.25+", agent.VERSION in ("1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"),
      repr(agent.VERSION))
check("manager >=1.0.2 gate accepts 1.0.25",
      app._version_at_least("1.0.25", "1.0.2"))
check("manager gate still rejects old-vs-new correctly",
      not app._version_at_least("1.0.24", "1.0.25")
      and app._version_at_least("1.0.24", "1.0.2"))

# ---- B. _sw_pid_alive ----
print("== B. _sw_pid_alive ==")
check("own pid is alive", agent._sw_pid_alive(os.getpid()))
pr = subprocess.Popen(["true"])
pr.wait()
dead_pid = pr.pid
check("dead pid is not alive", not agent._sw_pid_alive(dead_pid),
      repr(dead_pid))
check("garbage string not alive", not agent._sw_pid_alive("x"))
check("empty string not alive", not agent._sw_pid_alive(""))
check("negative pid not alive", not agent._sw_pid_alive(-5))
check("None not alive", not agent._sw_pid_alive(None))

# ---- C. sw_selfwatch_lock units ----
print("== C. sw_selfwatch_lock ==")
d = fresh_dir("wsl-lock-")
state, detail = agent.sw_selfwatch_lock(d)
check("fresh acquire -> held", state == "held", repr(state))
check("pidfile holds our pid",
      open(os.path.join(d, "selfwatch.lock", "pid")).read().strip()
      == str(os.getpid()))
state, detail = agent.sw_selfwatch_lock(d)
check("re-entrant acquire -> held, no takeover",
      state == "held" and detail.get("takeover") is None, repr((state, detail)))

# foreign: another live process (our parent) with a fresh lock
d2 = fresh_dir("wsl-foreign-")
os.mkdir(os.path.join(d2, "selfwatch.lock"))
with open(os.path.join(d2, "selfwatch.lock", "pid"), "w") as fh:
    fh.write(str(os.getppid()))
state, detail = agent.sw_selfwatch_lock(d2)
check("live foreign holder, fresh mtime -> foreign",
      state == "foreign" and detail.get("pid") == str(os.getppid()),
      repr((state, detail)))

# dead holder -> takeover
d3 = fresh_dir("wsl-dead-")
os.mkdir(os.path.join(d3, "selfwatch.lock"))
with open(os.path.join(d3, "selfwatch.lock", "pid"), "w") as fh:
    fh.write(str(dead_pid))
state, detail = agent.sw_selfwatch_lock(d3)
check("dead holder -> held with dead-takeover",
      state == "held" and detail.get("takeover")
      and "dead" in detail["takeover"], repr((state, detail)))
check("takeover rewrites pidfile to our pid",
      open(os.path.join(d3, "selfwatch.lock", "pid")).read().strip()
      == str(os.getpid()))

# stale holder: another LIVE pid (our parent) but an old mtime
d4 = fresh_dir("wsl-stale-")
os.mkdir(os.path.join(d4, "selfwatch.lock"))
with open(os.path.join(d4, "selfwatch.lock", "pid"), "w") as fh:
    fh.write(str(os.getppid()))
old = time.time() - 200
os.utime(os.path.join(d4, "selfwatch.lock"), (old, old))
state, detail = agent.sw_selfwatch_lock(d4)
check("stale holder -> held with stale-takeover",
      state == "held" and detail.get("takeover")
      and "stale" in detail["takeover"], repr((state, detail)))

# own pid, even with an old mtime, is re-entrant (we ARE the holder)
d4b = fresh_dir("wsl-selfstale-")
os.mkdir(os.path.join(d4b, "selfwatch.lock"))
with open(os.path.join(d4b, "selfwatch.lock", "pid"), "w") as fh:
    fh.write(str(os.getpid()))
os.utime(os.path.join(d4b, "selfwatch.lock"), (old, old))
state, detail = agent.sw_selfwatch_lock(d4b)
check("own pid, stale mtime -> held, re-entrant (no takeover)",
      state == "held" and detail.get("takeover") is None,
      repr((state, detail)))

# lockdir without a pidfile -> holder unknown -> takeover
d5 = fresh_dir("wsl-nopid-")
os.mkdir(os.path.join(d5, "selfwatch.lock"))
state, detail = agent.sw_selfwatch_lock(d5)
check("lockdir without pidfile -> held (takeover)",
      state == "held" and detail.get("takeover"), repr((state, detail)))

# broken: st_dir is a file
f6 = tempfile.NamedTemporaryFile(prefix="wsl-broken-", delete=False)
f6.close()
state, detail = agent.sw_selfwatch_lock(f6.name)
check("st_dir is a file -> broken",
      state == "broken" and "state dir" in detail.get("reason", ""),
      repr((state, detail)))
os.remove(f6.name)

# garbage st_dir -> broken, never raises
try:
    state, detail = agent.sw_selfwatch_lock(12345)
    check("garbage st_dir -> broken, never raises", state == "broken",
          repr(state))
except Exception as e:
    check("garbage st_dir -> broken, never raises", False, repr(e))

# lock path exists as a *file*: mkdir keeps failing -> broken, never hangs
d7 = fresh_dir("wsl-lockfile-")
open(os.path.join(d7, "selfwatch.lock"), "w").close()
state, detail = agent.sw_selfwatch_lock(d7)
check("lock path is a file -> broken (no hang)",
      state == "broken", repr((state, detail)))

# ---- D. once-only notes ----
print("== D. note helpers ==")
d8 = fresh_dir("wsl-notes-")
agent._SW_LOCK_FOREIGN_NOTED = False
agent._sw_lock_note_foreign(d8, "4242")
agent._sw_lock_note_foreign(d8, "4242")
lines = [l for l in log_lines(d8) if "another agent process holds" in l]
check("foreign note logged exactly once", len(lines) == 1, repr(lines))
check("foreign note names the pid", "4242" in lines[0] if lines else False)

agent._sw_lock_note_takeover(d8, "previous holder pid 4242 is dead")
check("takeover resets the foreign flag",
      agent._SW_LOCK_FOREIGN_NOTED is False)
agent._sw_lock_note_foreign(d8, "4343")
lines = [l for l in log_lines(d8) if "another agent process holds" in l]
check("foreign note fires again after takeover", len(lines) == 2,
      repr(len(lines)))
took = [l for l in log_lines(d8) if "taken over" in l]
check("takeover note says recovery resumed",
      took and "recovery resumed" in took[0], repr(took))

d9 = fresh_dir("wsl-brokenday-")
agent._SW_LOCK_BROKEN_DAY = ""
agent._sw_lock_note_broken(d9, "mkdir failed: boom", today="2026-10-19")
agent._sw_lock_note_broken(d9, "mkdir failed: boom", today="2026-10-19")
blines = [l for l in log_lines(d9) if "lock unusable" in l]
check("broken note once per day", len(blines) == 1, repr(blines))
agent._sw_lock_note_broken(d9, "mkdir failed: boom", today="2026-10-20")
blines = [l for l in log_lines(d9) if "lock unusable" in l]
check("broken note fires again next day", len(blines) == 2,
      repr(len(blines)))

# ---- E. selfwatch_pass_once gating ----
print("== E. selfwatch_pass_once ==")


def patch_all(calls):
    orig = {}
    names = ("load_instances", "selfwatch_check_instance", "sw_check_disk",
             "sw_trim_container_logs", "sw_ensure_loop_devices",
             "sw_check_memory", "sw_check_agent_http", "sw_boot_recovery",
             "sw_check_docker_daemon")
    for n in names:
        orig[n] = getattr(agent, n)
    agent.load_instances = lambda: {"i1": {}, "i2": {}}

    def fake_check_instance(name, **kw):
        calls.append(("instance", name))
        calls.append(("bootkw", kw.get("boot_recovery")))

    agent.selfwatch_check_instance = fake_check_instance

    def fake_boot_recovery(**kw):
        calls.append(("section0", "boot-recovery"))
        return "noop-sentinel"

    agent.sw_boot_recovery = fake_boot_recovery
    # (agent 1.0.39) the daemon guard runs first in every held pass: fake it
    # healthy, otherwise the real `docker info` probe fails in this env and
    # the per-instance loop + boot recovery below are (correctly) skipped.
    agent.sw_check_docker_daemon = lambda **kw: True
    agent.sw_check_disk = lambda **kw: calls.append(("guard", "disk"))
    agent.sw_trim_container_logs = lambda: calls.append(("guard", "trim"))
    agent.sw_ensure_loop_devices = lambda: calls.append(("guard", "loop"))
    agent.sw_check_memory = lambda: calls.append(("guard", "mem"))
    agent.sw_check_agent_http = lambda: calls.append(("http", "selfcheck"))
    return orig


def unpatch(orig):
    for n, fn in orig.items():
        setattr(agent, n, fn)


d10 = fresh_dir("wsl-pass-")
agent._SW_LOCK_FOREIGN_NOTED = False
agent._SW_LOCK_BROKEN_DAY = ""
calls = []
orig = patch_all(calls)
try:
    state = agent.selfwatch_pass_once(
        lock_fn=lambda: ("held", {"st_dir": d10, "pid": "1",
                                  "takeover": None}))
    check("held -> returns held", state == "held", repr(state))
    check("held -> per-instance checks ran",
          ("instance", "i1") in calls and ("instance", "i2") in calls,
          repr(calls))
    check("held -> section 0 (boot recovery) ran before the per-instance checks",
          ("section0", "boot-recovery") in calls
          and calls.index(("section0", "boot-recovery"))
          < calls.index(("instance", "i1")),
          repr(calls))
    check("held -> boot_recovery status threaded into check_instance",
          ("bootkw", "noop-sentinel") in calls, repr(calls))
    check("held -> all four host guards ran",
          all(("guard", g) in calls for g in ("disk", "trim", "loop", "mem")),
          repr(calls))
    check("held -> HTTP self-check ran", ("http", "selfcheck") in calls)

    calls.clear()
    agent._SW_LOCK_FOREIGN_NOTED = False
    state = agent.selfwatch_pass_once(
        lock_fn=lambda: ("foreign", {"st_dir": d10, "pid": "9999"}))
    kinds = {k for k, _ in calls}
    check("foreign -> returns foreign, recovery skipped",
          state == "foreign" and kinds == {"http"}, repr((state, calls)))
    check("foreign -> HTTP self-check still runs",
          ("http", "selfcheck") in calls)
    flines = [l for l in log_lines(d10) if "another agent process holds" in l]
    check("foreign -> exactly one WARN", len(flines) == 1, repr(flines))

    calls.clear()
    state = agent.selfwatch_pass_once(
        lock_fn=lambda: ("broken", {"st_dir": d10, "reason": "boom"}))
    kinds = {k for k, _ in calls}
    check("broken -> returns broken, recovery skipped",
          state == "broken" and kinds == {"http"}, repr((state, calls)))

    calls.clear()
    def raiser():
        raise RuntimeError("lock exploded")
    state = agent.selfwatch_pass_once(lock_fn=raiser)
    check("raising lock_fn -> broken, never raises", state == "broken",
          repr(state))
    state = agent.selfwatch_pass_once(lock_fn=lambda: None)
    check("garbage lock_fn result -> broken, never raises",
          state == "broken", repr(state))
finally:
    unpatch(orig)

# ---- F. wiring contracts ----
print("== F. wiring ==")
loop_src = inspect.getsource(agent.selfwatch_loop)
check("selfwatch_loop delegates to selfwatch_pass_once",
      "selfwatch_pass_once()" in loop_src)
check("selfwatch_loop has no inline guard calls",
      not any(tok in loop_src for tok in (
          "sw_check_disk(", "sw_trim_container_logs(",
          "sw_ensure_loop_devices(", "sw_check_memory(",
          "selfwatch_check_instance(")))
regs = dict((n, f) for n, f in agent._supervisor_registrations())
check("supervisor still registers the selfwatch thread",
      "selfwatch" in regs, repr(sorted(regs)))
pass_src = inspect.getsource(agent.selfwatch_pass_once)
check("HTTP self-check runs outside the lock branches",
      pass_src.count("sw_check_agent_http()") == 1
      and 'state == "held"' in pass_src)

# ---- G. thread churn smoke ----
print("== G. thread smoke ==")
d11 = fresh_dir("wsl-churn-")
errors = []
results = []


def churn():
    try:
        for _ in range(20):
            results.append(agent.sw_selfwatch_lock(d11)[0])
    except Exception as e:  # noqa: BLE001 - the test must not hide failures
        errors.append(e)


threads = [threading.Thread(target=churn) for _ in range(10)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("10 threads x 20 lock calls: no exceptions", not errors,
      repr(errors[:1]))
check("10 threads x 20 lock calls: all held", all(r == "held" for r in results)
      and len(results) == 200, repr(len(results)))

# ---- H. pidfile-grace race (1.0.25) ----
# The mkdir winner writes its pidfile in the syscalls right after mkdir. A
# loser arriving in that window used to read a missing pidfile, declare the
# holder dead, and rmdir() the winner's lock dir -- the winner's pidfile
# write then failed and it reported "broken" (false signal + WARN). The
# grace waits for the pidfile before treating the lock as abandoned.
print("== H. pidfile grace ==")
check("grace constant sane",
      0 < agent.SW_LOCK_PIDFILE_GRACE_S <= 5, repr(agent.SW_LOCK_PIDFILE_GRACE_S))

# Deterministic reproduction: a "winner" thread mkdirs the lock dir but
# sleeps before writing the pidfile; the main thread must wait out the
# grace and see "held" (same pid), never steal the lock.
d3 = fresh_dir("wsl-grace-")
lockdir3 = os.path.join(d3, "selfwatch.lock")
os.mkdir(lockdir3)
written = threading.Event()


def slow_winner():
    time.sleep(0.2)  # inside the 0.5s grace
    with open(os.path.join(lockdir3, "pid"), "w") as fh:
        fh.write(str(os.getpid()))
    written.set()


wt = threading.Thread(target=slow_winner)
wt.start()
t0 = time.time()
state3, detail3 = agent.sw_selfwatch_lock(d3)
dt3 = time.time() - t0
wt.join()
check("slow winner: waiter sees held, not takeover",
      state3 == "held", repr((state3, detail3)))
check("slow winner: no takeover recorded",
      not detail3.get("takeover"), repr(detail3))
check("slow winner: waited for the pidfile (grace engaged)",
      dt3 >= 0.15, repr(round(dt3, 3)))
check("slow winner: lock dir intact", os.path.isdir(lockdir3))

# A crashed winner (lock dir, never any pidfile) is still taken over
# after the grace -- the grace must not wedge recovery forever.
d4 = fresh_dir("wsl-grace-dead-")
os.mkdir(os.path.join(d4, "selfwatch.lock"))
t0 = time.time()
state4, detail4 = agent.sw_selfwatch_lock(d4)
dt4 = time.time() - t0
check("dead winner: taken over after grace",
      state4 == "held" and detail4.get("takeover"), repr((state4, detail4)))
check("dead winner: grace bounded",
      dt4 < agent.SW_LOCK_PIDFILE_GRACE_S + 2, repr(round(dt4, 3)))

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
