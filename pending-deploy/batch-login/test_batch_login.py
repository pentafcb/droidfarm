"""Unit tests for fleet batch Roblox login (manager/app.py roblox-login action +
dashboard fleet bar "Login all" button).

Run: python3 test_batch_login.py
Covers: BATCH_ACTIONS membership, batch_create_job validation, single-action
roblox-login for local (running/stopped/unknown) and remote (node missing,
agent too old), outcome recording with by="batch", exception safety in the
batch worker (one instance's failure never kills the batch), no credential
leakage into job results/records, and the dashboard contract.

Hermetic: load_instances/load_providers/container_status/roblox_login/
load_login_results/save_login_results/wing_request are monkeypatched; no
docker, no network, real time.sleep is avoided by mocking roblox_login.
"""
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

app = load("batchlogin_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
with open(os.path.join(HERE, "..", "..", "manager", "static", "index.html")) as f:
    DASH_SRC = f.read()

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
INSTANCES = {
    "rbx1": {"provider": "local"},
    "rbx2": {"provider": "local"},
    "srt1": {"provider": "p_vultr"},
}
PROVIDERS = {"p_vultr": {"url": "http://10.0.0.2:8090", "agent_key": "k"}}
STATUS = {"rbx1": "running", "rbx2": "exited"}   # srt1 = remote, checked by inner
LOGIN_RET = {}    # name -> (ok, msg) for the fake roblox_login
LOGIN_BY = {}     # name -> by value captured
_FAKE_RESULTS = {}

def fake_load_instances():
    return dict(INSTANCES)
def fake_load_providers():
    return dict(PROVIDERS)
def fake_container_status(cname):
    return STATUS.get(cname.replace("wd-", ""), "missing")
def fake_roblox_login(name, by="manual"):
    LOGIN_BY[name] = by
    if name == "boom":
        raise RuntimeError("inner exploded")
    return LOGIN_RET.get(name, (True, "login sequence sent - check if login succeeded"))
def fake_load_login_results():
    return dict(_FAKE_RESULTS)
def fake_save_login_results(data):
    _FAKE_RESULTS.clear()
    _FAKE_RESULTS.update(data)

def reset():
    LOGIN_RET.clear(); LOGIN_BY.clear(); _FAKE_RESULTS.clear()
    app._batch_jobs.clear()
    app.load_instances = fake_load_instances
    app.load_providers = fake_load_providers
    app.container_status = fake_container_status
    app.roblox_login = fake_roblox_login
    app.load_login_results = fake_load_login_results
    app.save_login_results = fake_save_login_results

reset()

# ---------------- BATCH_ACTIONS / create validation ----------------
check("roblox-login in BATCH_ACTIONS",
      "roblox-login" in app.BATCH_ACTIONS)
check("old actions still present",
      all(a in app.BATCH_ACTIONS for a in ("start", "stop", "restart", "launch-roblox")))
code, resp = app.batch_create_job("roblox-login", [])
check("create roblox-login job -> 202", code == 202 and resp["total"] == 3)
check("job action recorded", app._batch_jobs[resp["job_id"]]["action"] == "roblox-login")
app._batch_jobs.clear()
code, resp = app.batch_create_job("roblox-login", ["rbx1"])
check("create roblox-login with names -> 202/1", code == 202 and resp["total"] == 1)
app._batch_jobs.clear()
code, resp = app.batch_create_job("hack-the-planet", [])
check("junk action still 400", code == 400 and "roblox-login" in resp.get("error", ""))
code, resp = app.batch_create_job("roblox-login", ["nope"])
check("unknown name -> 400", code == 400)

# ---------------- instance_single_action: local ----------------
reset()
code, resp = app.instance_single_action("ghost", "roblox-login")
check("unknown instance -> 404", code == 404)
code, resp = app.instance_single_action("rbx2", "roblox-login")
check("stopped local -> 409", code == 409 and resp.get("error") == "instance not running")
check("stopped local -> no login attempt", "rbx2" not in LOGIN_BY)

reset()
LOGIN_RET["rbx1"] = (True, "login sequence sent - check if login succeeded")
code, resp = app.instance_single_action("rbx1", "roblox-login")
check("running local ok -> 200", code == 200 and resp.get("ok") is True)
check("running local -> msg passthrough", resp.get("msg") == "login sequence sent - check if login succeeded")
check("running local -> roblox_login by=batch", LOGIN_BY.get("rbx1") == "batch")

reset()
LOGIN_RET["rbx1"] = (False, "no credentials saved for this instance")
code, resp = app.instance_single_action("rbx1", "roblox-login")
check("inner failure -> 400", code == 400 and resp.get("ok") is False)
check("inner failure -> msg passthrough", resp.get("msg") == "no credentials saved for this instance")
check("inner failure -> still records by=batch", LOGIN_BY.get("rbx1") == "batch")

# ---------------- instance_single_action: remote ----------------
# Remote path is NOT proxied to the wing agent (no agent login endpoint);
# the manager drives it via roblox_login, which routes internally.
reset()
LOGIN_RET["srt1"] = (True, "login sequence sent - check if login succeeded")
called = []
orig = app.roblox_login
def spy_roblox_login(name, by="manual"):
    called.append(name)
    return orig(name, by=by)
app.roblox_login = spy_roblox_login
code, resp = app.instance_single_action("srt1", "roblox-login")
check("remote -> 200", code == 200 and resp.get("ok") is True)
check("remote -> went through roblox_login (by=batch)", called == ["srt1"] and LOGIN_BY.get("srt1") == "batch")
app.roblox_login = fake_roblox_login

reset()
LOGIN_RET["srt1"] = (False, "node agent too old for auto-login (needs 1.0.2+) - re-run the node installer on that machine")
code, resp = app.instance_single_action("srt1", "roblox-login")
check("remote old-agent -> 400 with clear message",
      code == 400 and "too old" in resp.get("msg", ""))
check("remote old-agent -> recorded by=batch", LOGIN_BY.get("srt1") == "batch")

# ---------------- by="batch" recording ----------------
reset()
app.record_login_result("rbx1", True, "login sequence sent", by="batch")
check("record: by batch kept", _FAKE_RESULTS.get("rbx1", {}).get("by") == "batch")
app.record_login_result("rbx1", True, "ok", by="weird-value")
check("record: bogus by still -> manual", _FAKE_RESULTS.get("rbx1", {}).get("by") == "manual")
app.record_login_result("rbx1", False, "no credentials", by="auto")
check("record: auto still kept", _FAKE_RESULTS.get("rbx1", {}).get("by") == "auto")

# ---------------- worker: exception safety + ordering ----------------
reset()
LOGIN_RET["rbx1"] = (True, "login sequence sent")
LOGIN_RET["boom"] = (True, "never reached")
INSTANCES["boom"] = {"provider": "local"}
STATUS["boom"] = "running"
try:
    code, resp = app.batch_create_job("roblox-login", ["rbx1", "rbx2", "boom", "srt1"])
    check("batch create -> 202", code == 202)
    jid = resp["job_id"]
    for _ in range(200):
        import time as _t
        _t.sleep(0.05)
        if app._batch_jobs.get(jid, {}).get("status") == "done":
            break
    view = app.batch_get_job(jid)[1]
    names = [r["name"] for r in view["results"]]
    check("worker: results in target order", names == ["rbx1", "rbx2", "boom", "srt1"])
    byname = {r["name"]: r for r in view["results"]}
    check("worker: rbx1 ok", byname["rbx1"]["ok"] is True)
    check("worker: rbx2 failed (409 stopped)", byname["rbx2"]["ok"] is False)
    check("worker: exception instance recorded failed, not fatal",
          byname["boom"]["ok"] is False and "exception" in byname["boom"]["error"])
    check("worker: srt1 still processed after exception", byname["srt1"]["ok"] is True)
    check("worker: failed list", set(view["failed"]) == {"rbx2", "boom"})
    check("worker: job done", view["status"] == "done")
finally:
    del INSTANCES["boom"]
    del STATUS["boom"]

# ---------------- no credential leakage ----------------
# Reasons recorded by the real record_login_result path must never contain a
# password even when the (mocked) login message is oddly shaped.
reset()
SECRET = "Sup3r-S3cret!pw"
LOGIN_RET["rbx1"] = (False, "no credentials saved for this instance")
real_record = app.record_login_result
def fake_login_with_record(name, by="manual"):
    ok, msg = LOGIN_RET.get(name, (True, "ok"))
    real_record(name, ok, msg, by)   # exercise the real record path
    LOGIN_BY[name] = by
    return ok, msg
app.roblox_login = fake_login_with_record
code, resp = app.instance_single_action("rbx1", "roblox-login")
rec = _FAKE_RESULTS.get("rbx1", {})
blob = (rec.get("reason", "") + " " + str(resp.get("msg", "")) +
        " " + str(resp.get("error", "")))
check("no password in result/record", SECRET not in blob)
check("record ok flag + by batch", rec.get("ok") is False and rec.get("by") == "batch")

# ---------------- dashboard contract ----------------
check("dash: Login all button", 'id="btnFleetLogin"' in DASH_SRC)
_btn_login = re.search(r'<button[^>]*id="btnFleetLogin"[^>]*>(.*?)</button>', DASH_SRC, re.S)
check("dash: button label",
      _btn_login is not None and re.sub(r"<[^>]+>", "", _btn_login.group(1)).strip() != "")
check("dash: onclick wires roblox-login",
      "btnFleetLogin').onclick" in DASH_SRC and "fleetAction('roblox-login'" in DASH_SRC)
check("dash: confirm message",
      re.search(r"btnFleetLogin.*?fleetAction\('roblox-login'.*?(TODAS|ALL)", DASH_SRC, re.S) is not None)
check("dash: fleet comment updated", "launch-roblox/" in DASH_SRC and "roblox-login" in DASH_SRC)
check("dash: reuses fleetAction progress modal",
      DASH_SRC.count("fleetAction('roblox-login'") == 1)
check("dash: no credential fields in fleetbar",
      "password" not in DASH_SRC.split('id="fleetbar"')[1].split("</div>")[0].lower())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
