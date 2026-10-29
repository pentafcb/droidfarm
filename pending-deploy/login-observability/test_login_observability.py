"""Unit tests for Roblox auto-login result tracking (manager/app.py + dashboard).

Run: python3 test_login_observability.py
Covers: record_login_result (sanitize, truncation, attempts, by-coercion,
corrupt-file tolerance, never-raises, 600 perms), roblox_login wrapper
(outcome recorded with correct `by`), roblox_account_summary (coverage +
logged_in count, no passwords), /api/instances contract (login fields on
both branches), roblox-login-result route placement/auth, and the dashboard
contract (card badge, coverage line, Accounts modal list, viewer panel).

Hermetic: load_login_results/save_login_results are monkeypatched or pointed
at tmp files; no docker, no network, no writes to the real data dir.
"""
import importlib.util
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

app = load("loginobs_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
APP_SRC = open(os.path.join(HERE, "..", "..", "manager", "app.py")).read()
DASH_SRC = open(os.path.join(HERE, "..", "..", "manager", "static", "index.html")).read()

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")

# ---------------- fakes ----------------
_FAKE_RESULTS = {}
_SAVED = {"count": 0}

def fake_load_login_results():
    return dict(_FAKE_RESULTS)

def fake_save_login_results(data):
    _SAVED["count"] += 1
    _FAKE_RESULTS.clear()
    _FAKE_RESULTS.update(data)

_REAL_LOAD = app.load_login_results
_REAL_SAVE = app.save_login_results
app.load_login_results = fake_load_login_results
app.save_login_results = fake_save_login_results

_FAKE_ACCOUNTS = {}
_FAKE_INSTANCES = {"rbx1": {}, "rbx2": {}, "srt1": {}}
app.load_accounts = lambda: dict(_FAKE_ACCOUNTS)
app.load_instances = lambda: dict(_FAKE_INSTANCES)

def reset():
    _FAKE_RESULTS.clear()
    _SAVED["count"] = 0

# ---------------- record_login_result ----------------
reset()
before = int(time.time())
app.record_login_result("rbx1", True, "login sequence sent", by="auto")
r = _FAKE_RESULTS.get("rbx1")
check("record: entry created", r is not None)
check("record: ok True", r and r["ok"] is True)
check("record: by auto kept", r and r["by"] == "auto")
check("record: attempts starts at 1", r and r["attempts"] == 1)
check("record: ts sane", r and abs(r["ts"] - before) < 5)
check("record: reason kept", r and r["reason"] == "login sequence sent")

app.record_login_result("rbx1", False, "remote input failed", by="manual")
r = _FAKE_RESULTS.get("rbx1")
check("record: attempts increments", r and r["attempts"] == 2)
check("record: last result wins", r and r["ok"] is False and r["by"] == "manual")

app.record_login_result("rbx2", True, "x" * 500)
r2 = _FAKE_RESULTS.get("rbx2")
check("record: reason truncated to 160", r2 and len(r2["reason"]) == 160)

app.record_login_result("rbx3", True, "ok", by="weird-value")
check("record: bogus by -> manual", _FAKE_RESULTS["rbx3"]["by"] == "manual")
app.record_login_result("rbx3", 0, None)
check("record: falsy ok -> False, None reason -> ''",
      _FAKE_RESULTS["rbx3"]["ok"] is False and _FAKE_RESULTS["rbx3"]["reason"] == "")

reset()
app.record_login_result("../../../etc/x", True, "ok")
check("record: traversal name sanitized", "etx" in _FAKE_RESULTS or "etcx" in _FAKE_RESULTS)
check("record: no unsanitized key", all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", k) for k in _FAKE_RESULTS))
n0 = _SAVED["count"]
app.record_login_result("!!!", True, "ok")
check("record: empty-after-sanitize skipped (no write)", _SAVED["count"] == n0)

# never raises, even if the save path explodes
app.save_login_results = lambda d: (_ for _ in ()).throw(OSError("disk gone"))
try:
    app.record_login_result("rbx9", True, "ok")
    raised = False
except Exception:
    raised = True
check("record: save failure never raises", not raised)
app.save_login_results = fake_save_login_results

# corrupt on-disk JSON tolerated (point at a real tmp file)
import tempfile
tmp = tempfile.NamedTemporaryFile("w", delete=False)
tmp.write("{not valid json!!!")
tmp.close()
orig_file = app.LOGIN_RESULTS_FILE
app.LOGIN_RESULTS_FILE = tmp.name
# use the REAL loader/saver for this on-disk round trip
app.load_login_results = _REAL_LOAD
app.save_login_results = _REAL_SAVE
try:
    check("load: corrupt file -> {}", app.load_login_results() == {})
    app.record_login_result("rbx1", True, "ok")
    d = json.load(open(tmp.name))
    check("load: recovery writes valid JSON", d["rbx1"]["ok"] is True)
    mode = os.stat(tmp.name).st_mode & 0o777
    check("save: file perms 600", mode == 0o600)
finally:
    app.LOGIN_RESULTS_FILE = orig_file
    app.load_login_results = fake_load_login_results
    app.save_login_results = fake_save_login_results
    os.unlink(tmp.name)

# ---------------- roblox_login wrapper ----------------
reset()
calls = []
def fake_inner(name):
    calls.append(name)
    return True, "inner says hi"
app._roblox_login_inner = fake_inner
ok, msg = app.roblox_login("rbx1", by="auto")
check("wrapper: returns inner result", ok is True and msg == "inner says hi")
check("wrapper: inner got name", calls == ["rbx1"])
r = _FAKE_RESULTS.get("rbx1")
check("wrapper: outcome recorded with by=auto", r and r["ok"] is True and r["by"] == "auto")

ok, msg = app.roblox_login("rbx2")
check("wrapper: default by=manual", _FAKE_RESULTS["rbx2"]["by"] == "manual")

def fake_inner_fail(name):
    return False, "no credentials saved for this instance"
app._roblox_login_inner = fake_inner_fail
ok, msg = app.roblox_login("rbx3", by="auto")
check("wrapper: failure recorded too",
      ok is False and _FAKE_RESULTS["rbx3"]["ok"] is False)

# ---------------- roblox_account_summary ----------------
reset()
_FAKE_ACCOUNTS.clear()
_FAKE_ACCOUNTS.update({
    "rbx1": {"username": "u1", "password": "p1", "auto_login": True},
    "rbx2": {"username": "u2", "password": "p2"},
})
app.record_login_result("rbx1", True, "sent", by="auto")
app.record_login_result("rbx2", False, "remote input failed: timeout", by="manual")
s = app.roblox_account_summary()
check("summary: covered 2", s["covered"] == 2 and s["total"] == 3)
check("summary: logged_in counts only True", s["logged_in"] == 1)
check("summary: per-account login fields",
      s["accounts"]["rbx1"]["login_ok"] is True
      and s["accounts"]["rbx1"]["login_by"] == "auto"
      and s["accounts"]["rbx2"]["login_ok"] is False
      and s["accounts"]["rbx2"]["login_reason"] == "remote input failed: timeout"
      and s["accounts"]["srt1"]["login_ok"] is None
      and s["accounts"]["srt1"]["login_attempts"] == 0)
blob = json.dumps(s)
check("summary: no password key/value anywhere",
      '"password"' not in blob and '"p1"' not in blob and '"p2"' not in blob)

# ---------------- /api/instances contract ----------------
# identical dict-key pattern used in 3 places: remote branch, local branch,
# roblox_account_summary. Both /api/instances branches must carry it.
check("instances route: login_ok present on both branches",
      APP_SRC.count('"login_ok": lr.get("ok") if lr else None') == 3)
check("instances route: login_ts on both branches",
      APP_SRC.count('"login_ts": lr.get("ts") if lr else None') == 3)
check("instances route: login_by on both branches",
      APP_SRC.count('"login_by": lr.get("by") if lr else None') == 3)
check("instances route: results loaded once before loop",
      "login_results = load_login_results()" in APP_SRC)

# ---------------- roblox-login-result route ----------------
m = re.search(r'/roblox-login-result", path\)', APP_SRC)
check("route: roblox-login-result registered in do_GET", m is not None)
seg = APP_SRC[m.start():m.start() + 600] if m else ""
check("route: auth-gated (401)", "_authed()" in seg and "401" in seg)
check("route: name sanitized before lookup",
      "valid_instance_name(name)" in seg)
check("route: returns null for unknown instance", '"result": lr if isinstance(lr, dict) else None' in seg)
check("route: never returns credentials", "password" not in seg.lower())

# ---------------- dashboard contract ----------------
check("dash: .loginstat CSS (ok/fail)", ".loginstat.ok" in DASH_SRC and ".loginstat.fail" in DASH_SRC)
check("dash: card renders login badge from i.login_ok/i.login_ts", "i.login_ok" in DASH_SRC and "i.login_ts" in DASH_SRC)
check("dash: failed badge mentions Accounts panel",
      re.search(r"Accounts panel|panel de [Cc]uentas", DASH_SRC) is not None)
check("dash: coverage shows logged_in count",
      re.search(r"\$\('acctCov'\)\.textContent\s*=[^;]*s\.logged_in", DASH_SRC) is not None)
check("dash: acctLoginRes element + render fn", 'id="acctLoginRes"' in DASH_SRC and "renderAcctLoginRes" in DASH_SRC)
check("dash: modal lists per-instance reason/time", "login_reason" in DASH_SRC and "renderAcctLoginRes" in DASH_SRC)
check("dash: viewer account panel fetches login-result",
      "/roblox-login-result" in DASH_SRC
      and re.search(r"lr\.result\.ok\s*\?\s*`[^`]*`\s*:\s*`[^`]*`", DASH_SRC) is not None)
check("dash: badge fields escaped (no injection)", DASH_SRC.count("esc(i.login_by") >= 2)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
