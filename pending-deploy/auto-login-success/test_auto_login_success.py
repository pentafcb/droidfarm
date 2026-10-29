"""Unit tests for auto-login success short-circuit + in-flight guard
(manager/app.py).

Run: python3 test_auto_login_success.py

REAL BUG FIXED:
  auto_login_watcher() fired a Roblox login sequence on an instance's
  first boot up to 3 times (its documented "max 3 attempts" cap) but
  NEVER checked whether an attempt had already succeeded: it never read
  roblox_login_results.json. So a first boot ALWAYS burned all 3
  attempts even when attempt 1 logged in fine - attempts 2 and 3 then
  re-tapped the username field (512,228), password field (512,296) and
  Sign In (512,367) coordinates into whatever screen Roblox was showing
  post-login (game UI, not the login form). A second, related gap: the
  watcher loop runs every 60s but a login sequence can take longer
  (waydroid_exec timeouts), so a second sequence could start while the
  first was still tapping, interleaving two tap streams.

Fix (manager/app.py only):
  * Success short-circuit: before firing, the pass checks the recorded
    login result for the instance; ok=True with ts >= this boot's start
    marks login_boot = started and fires nothing. A result from an older
    boot always has ts < started, so it never short-circuits a fresh
    boot. Manual/batch successes count too - logged in is logged in.
  * In-flight guard: _auto_login_inflight (set, _lock-guarded) holds
    names with a sequence running; the pass skips those without burning
    an attempt. _auto_login_run() discards its name in a finally block
    (success, failure, or exception), and a Thread.start() failure also
    releases the guard, so no path can wedge a boot.
  * Testability: the pass body moved to _auto_login_pass() (same code,
    same never-raises contract); auto_login_watcher() is now just
    sleep(90) + loop { _auto_login_pass(); sleep(60) }.

Hermetic: app.py loaded via importlib; all docker/waydroid/network
dependencies faked (load_accounts, load_instances, load_providers,
load_login_results, _container_started_at, _remote_started_at,
check_booted, _roblox_installed, wing_request, roblox_login); the
spawned threads are faked (recorded, never run unless the test runs
them) so no real login sequence ever executes.
"""
import copy
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("autologinsucc_app",
           os.path.join(HERE, "..", "..", "manager", "app.py"))
with open(os.path.join(HERE, "..", "..", "manager", "app.py")) as f:
    APP_SRC = f.read()

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


STARTED = 1700000000  # fake container StartedAt (epoch seconds)


class FakeThread:
    """Records spawns; start() does NOT run the target (test drives it)."""
    spawned = []
    raise_on_start = False

    def __init__(self, target=None, args=(), daemon=None, name=None):
        self.target = target
        self.args = args or ()
        self.name = name

    def start(self):
        if FakeThread.raise_on_start:
            raise RuntimeError("boom")
        FakeThread.spawned.append(self)


REAL_THREAD = app.threading.Thread
app.threading.Thread = FakeThread

saved = []          # save_accounts() captures, in order
login_calls = []    # roblox_login() calls


def fake_login(name, by="manual"):
    login_calls.append((name, by))
    return fake_login.result


fake_login.result = (True, "ok")


def reset(accounts, instances, results, providers=None):
    FakeThread.spawned = []
    FakeThread.raise_on_start = False
    saved.clear()
    login_calls.clear()
    fake_login.result = (True, "ok")
    app._auto_login_inflight.clear()
    app.load_accounts = lambda: copy.deepcopy(accounts)
    app.load_instances = lambda: copy.deepcopy(instances)
    app.load_providers = lambda: copy.deepcopy(providers or {})
    app.load_login_results = lambda: copy.deepcopy(results)
    app.save_accounts = lambda data: saved.append(copy.deepcopy(data))
    app._container_started_at = lambda cname: STARTED
    app._remote_started_at = lambda p, name: STARTED
    app.check_booted = lambda name: True
    app._roblox_installed = lambda name: True
    app.wing_calls = []
    def fake_wing(p, method, path, data=None, timeout=None):
        app.wing_calls.append(path)
        return (200, {"setup": "ready", "roblox": True})
    app.wing_request = fake_wing
    app.roblox_login = fake_login


def base_acct(**over):
    d = {"auto_login": True, "username": "player_one", "password": "s3cret"}
    d.update(over)
    return d


def base_inst(**over):
    d = {"provider": "local", "created_at": STARTED - 600}
    d.update(over)
    return d


def spawns():
    return [t for t in FakeThread.spawned if t.target is app._auto_login_run]


# --- 1. success short-circuit -------------------------------------------
reset({"rbx1": base_acct()},
      {"rbx1": base_inst()},
      {"rbx1": {"ok": True, "ts": STARTED + 120, "by": "auto",
                "reason": "ok", "attempts": 1}})
app._auto_login_pass()
check("success this boot: no sequence spawned", len(spawns()) == 0)
check("success this boot: login_boot marked",
      saved and saved[-1]["rbx1"].get("login_boot") == STARTED)
check("success this boot: attempts untouched",
      saved and "login_attempts" not in saved[-1]["rbx1"])

# --- 2. manual success counts too ----------------------------------------
reset({"rbx1": base_acct()},
      {"rbx1": base_inst()},
      {"rbx1": {"ok": True, "ts": STARTED + 5, "by": "manual",
                "reason": "ok", "attempts": 1}})
app._auto_login_pass()
check("manual success this boot: no sequence spawned", len(spawns()) == 0)
check("manual success this boot: login_boot marked",
      saved and saved[-1]["rbx1"].get("login_boot") == STARTED)

# --- 3. older-boot success does NOT short-circuit -------------------------
reset({"rbx1": base_acct()},
      {"rbx1": base_inst()},
      {"rbx1": {"ok": True, "ts": STARTED - 3600, "by": "auto",
                "reason": "ok", "attempts": 1}})
app._auto_login_pass()
check("older-boot success: sequence spawned", len(spawns()) == 1)
check("older-boot success: attempt counted",
      saved and saved[-1]["rbx1"].get("login_attempts") == 1)

# --- 4. failed result does NOT short-circuit ------------------------------
reset({"rbx1": base_acct()},
      {"rbx1": base_inst()},
      {"rbx1": {"ok": False, "ts": STARTED + 120, "by": "auto",
                "reason": "login failed", "attempts": 1}})
app._auto_login_pass()
check("failed result this boot: sequence spawned", len(spawns()) == 1)
check("failed result this boot: attempt counted",
      saved and saved[-1]["rbx1"].get("login_attempts") == 1)

# --- 5. malformed results never raise, never short-circuit -----------------
for label, bad in [("missing ts", {"ok": True, "by": "auto"}),
                   ("string ts", {"ok": True, "ts": "soon", "by": "auto"}),
                   ("non-dict", "garbage"),
                   ("absent", None)]:
    res = {} if bad is None else {"rbx1": bad}
    reset({"rbx1": base_acct()}, {"rbx1": base_inst()}, res)
    try:
        app._auto_login_pass()
        raised = False
    except Exception:
        raised = True
    check(f"malformed result ({label}): no raise, spawns",
          not raised and len(spawns()) == 1)

# --- 6. no result at all: normal first attempt -----------------------------
reset({"rbx1": base_acct()}, {"rbx1": base_inst()}, {})
app._auto_login_pass()
check("no result: sequence spawned", len(spawns()) == 1)
check("no result: attempt counted",
      saved and saved[-1]["rbx1"].get("login_attempts") == 1)

# --- 7. in-flight guard: skip without burning an attempt -------------------
reset({"rbx1": base_acct(login_attempts=1)},
      {"rbx1": base_inst()}, {})
app._auto_login_inflight.add("rbx1")
app._auto_login_pass()
check("in-flight: no second sequence spawned", len(spawns()) == 0)
check("in-flight: attempt NOT burned (still 1)",
      not saved or saved[-1]["rbx1"].get("login_attempts") == 1)

# --- 8. in-flight lifecycle: run clears the guard --------------------------
reset({"rbx1": base_acct()}, {"rbx1": base_inst()}, {})
app._auto_login_pass()
check("lifecycle: guard held after spawn", "rbx1" in app._auto_login_inflight)
t = spawns()[0]
t.target(*t.args)  # run the real _auto_login_run with fake roblox_login
check("lifecycle: guard cleared after run",
      "rbx1" not in app._auto_login_inflight)
check("lifecycle: login actually ran", login_calls == [("rbx1", "auto")])

# --- 9. exception inside the sequence still clears the guard ---------------
def boom_login(name, by="manual"):
    login_calls.append((name, by))
    raise RuntimeError("waydroid exploded")
reset({"rbx1": base_acct()}, {"rbx1": base_inst()}, {})
app.roblox_login = boom_login
app._auto_login_pass()
t = spawns()[0]
t.target(*t.args)  # _auto_login_run catches; finally must clear
check("exception run: guard cleared",
      "rbx1" not in app._auto_login_inflight)
check("exception run: pass itself never raised (reached here)", True)

# --- 10. Thread.start failure releases the guard, next pass retries --------
reset({"rbx1": base_acct()}, {"rbx1": base_inst()}, {})
FakeThread.raise_on_start = True
app._auto_login_pass()
check("start failure: guard released",
      "rbx1" not in app._auto_login_inflight)
check("start failure: attempt was counted before the failure",
      saved and saved[-1]["rbx1"].get("login_attempts") == 1)
FakeThread.raise_on_start = False
# second pass: same accounts but attempts now 1 (as saved) - retry works
reset({"rbx1": base_acct(login_attempts=1)},
      {"rbx1": base_inst()}, {})
app._auto_login_pass()
check("start failure: next pass can spawn again", len(spawns()) == 1)

# --- 11. pre-existing bounds unchanged --------------------------------------
reset({"rbx1": base_acct(login_attempts=3)},
      {"rbx1": base_inst()}, {})
app._auto_login_pass()
check("attempts exhausted: no spawn, boot marked",
      len(spawns()) == 0 and saved
      and saved[-1]["rbx1"].get("login_boot") == STARTED)

reset({"rbx1": base_acct()},
      {"rbx1": base_inst(created_at=STARTED - 3600)}, {})
app._auto_login_pass()
check("restart (>30min): no spawn, boot marked as restart",
      len(spawns()) == 0 and saved
      and saved[-1]["rbx1"].get("login_boot") == STARTED)

reset({"rbx1": base_acct()},
      {"rbx1": base_inst()}, {})
app._container_started_at = lambda cname: 0  # container not running
app._auto_login_pass()
check("container not started: no spawn", len(spawns()) == 0)

# --- 12. remote path: short-circuit skips the status call -------------------
reset({"rbx1": base_acct()},
      {"rbx1": base_inst(provider="node1")},
      {"rbx1": {"ok": True, "ts": STARTED + 30, "by": "batch",
                "reason": "ok", "attempts": 1}},
      providers={"node1": {"name": "srt"}})
app._auto_login_pass()
check("remote success: no spawn", len(spawns()) == 0)
check("remote success: no wing status call",
      len(app.wing_calls) == 0)
check("remote success: boot marked",
      saved and saved[-1]["rbx1"].get("login_boot") == STARTED)

reset({"rbx1": base_acct()},
      {"rbx1": base_inst(provider="node1")},
      {},
      providers={"node1": {"name": "srt"}})
app._auto_login_pass()
check("remote no result: spawn after ready status",
      len(spawns()) == 1 and len(app.wing_calls) == 1)

# --- 13. pass never raises ---------------------------------------------------
def bad_load():
    raise RuntimeError("state unreadable")
app.load_accounts = bad_load
try:
    app._auto_login_pass()
    raised = False
except Exception:
    raised = True
check("load_accounts raising: pass never raises", not raised)

# --- 14. source contracts -----------------------------------------------------
check("watcher delegates to _auto_login_pass",
      "def _auto_login_pass" in APP_SRC
      and "_auto_login_pass()" in APP_SRC)
check("pass reads login results",
      "load_login_results()" in APP_SRC)
check("run clears guard in finally",
      "_auto_login_inflight.discard(name)" in APP_SRC)
check("old loop shape gone (no inline while-True pass body)",
      APP_SRC.count("def auto_login_watcher") == 1)

app.threading.Thread = REAL_THREAD
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
