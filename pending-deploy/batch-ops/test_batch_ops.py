"""Unit tests for fleet batch operations (manager/app.py batch engine +
dashboard fleet bar).

Run: python3 test_batch_ops.py
Covers: instance_single_action for local/remote/both, batch job lifecycle
(create/validate/run/progress/delete/TTL), single-job concurrency guard,
route-regex placement, and dashboard fleet-bar contract.

Hermetic: load_instances/load_providers/run/container_status/check_roblox/
launch_roblox/wing_request are all monkeypatched; no docker, no network.
"""
import importlib.util
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

app = load("batchops_app", os.path.join(HERE, "..", "..", "manager", "app.py"))

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
CALLS = {"run": [], "wing": [], "launch": []}
INSTANCES = {
    "rbx1": {"provider": "local"},
    "rbx2": {"provider": "local"},
    "srt1": {"provider": "p_vultr"},
}
PROVIDERS = {"p_vultr": {"url": "http://10.0.0.2:8090", "agent_key": "k"}}
STATUS = {"rbx1": "running", "rbx2": "exited"}
ROBLOX = {"rbx1": True, "rbx2": True}
WING_RESP = (200, {"ok": True})

def fake_load_instances():
    return dict(INSTANCES)
def fake_load_providers():
    return dict(PROVIDERS)
def fake_run(cmd, timeout=30):
    CALLS["run"].append(cmd)
    return 0, "", ""
def fake_run_fail(cmd, timeout=30):
    CALLS["run"].append(cmd)
    return 1, "", "boom"
def fake_container_status(name):
    return STATUS.get(name.replace("wd-", ""), "missing")
def fake_check_roblox(name):
    return ROBLOX.get(name, False)
def fake_launch_roblox(name):
    CALLS["launch"].append(name)
    return True
def fake_wing_request(p, method, path, data=None, timeout=8):
    CALLS["wing"].append((method, path, timeout))
    return WING_RESP

def reset():
    CALLS["run"].clear(); CALLS["wing"].clear(); CALLS["launch"].clear()
    app._batch_jobs.clear()
    app.load_instances = fake_load_instances
    app.load_providers = fake_load_providers
    app.run = fake_run
    app.container_status = fake_container_status
    app.check_roblox = fake_check_roblox
    app.launch_roblox = fake_launch_roblox
    app.wing_request = fake_wing_request
    # binder-safe-start: the real pool prepare would shell out through the
    # fake run() and pollute the exact CALLS["run"] assertions below.
    app._prepare_pools_for_start = lambda: True

reset()

print("== instance_single_action: local ==")
code, resp = app.instance_single_action("rbx1", "start")
check("local start -> 200 + docker start wd-rbx1",
      code == 200 and CALLS["run"] == ["docker start wd-rbx1"])
reset()
code, resp = app.instance_single_action("rbx2", "stop")
check("local stop -> 200 + docker stop wd-rbx2",
      code == 200 and CALLS["run"] == ["docker stop wd-rbx2"])
reset()
code, resp = app.instance_single_action("rbx1", "restart")
check("local restart -> stop then start, in order",
      code == 200 and CALLS["run"] == ["docker stop wd-rbx1", "docker start wd-rbx1"])
reset()
app.run = fake_run_fail
code, resp = app.instance_single_action("rbx1", "start")
check("docker failure -> 500 with error text",
      code == 500 and resp.get("error") == "boom")
reset()

print("== instance_single_action: remote ==")
code, resp = app.instance_single_action("srt1", "start")
check("remote start proxied to wing agent (timeout 60)",
      code == 200 and CALLS["wing"] == [("POST", "/api/agent/instances/srt1/start", 60)])
reset()
code, resp = app.instance_single_action("srt1", "restart")
check("remote restart -> wing stop then wing start",
      code == 200 and CALLS["wing"] == [
          ("POST", "/api/agent/instances/srt1/stop", 60),
          ("POST", "/api/agent/instances/srt1/start", 60)])
reset()
code, resp = app.instance_single_action("srt1", "launch-roblox")
check("remote launch-roblox proxied (timeout 30)",
      code == 200 and CALLS["wing"] == [("POST", "/api/agent/instances/srt1/launch-roblox", 30)])
reset()
INSTANCES["srt1"] = {"provider": "p_gone"}
code, resp = app.instance_single_action("srt1", "start")
check("remote node missing from providers -> 404 node not found",
      code == 404 and resp.get("error") == "node not found")
INSTANCES["srt1"] = {"provider": "p_vultr"}
reset()

print("== instance_single_action: launch-roblox local guards ==")
code, resp = app.instance_single_action("rbx2", "launch-roblox")
check("not running -> 409 instance not running",
      code == 409 and resp.get("error") == "instance not running" and not CALLS["launch"])
ROBLOX["rbx1"] = False
code, resp = app.instance_single_action("rbx1", "launch-roblox")
check("roblox not installed -> 404", code == 404 and not CALLS["launch"])
ROBLOX["rbx1"] = True
code, resp = app.instance_single_action("rbx1", "launch-roblox")
check("happy path -> 200 {ok:True}, launch_roblox called once",
      code == 200 and resp == {"ok": True} and CALLS["launch"] == ["rbx1"])
code, resp = app.instance_single_action("nope", "start")
check("unknown instance -> 404", code == 404)
code, resp = app.instance_single_action("rbx1", "explode")
check("unknown action -> 400", code == 400)

print("== batch_create_job validation ==")
reset()
code, resp = app.batch_create_job("explode", [])
check("bad action -> 400 listing valid actions",
      code == 400 and "start" in resp.get("error", ""))
code, resp = app.batch_create_job("start", ["rbx1", "ghost"])
check("unknown name -> 400 naming it", code == 400 and "ghost" in resp.get("error", ""))
code, resp = app.batch_create_job("stop", [])
check("empty names -> all instances, 202 + job_id",
      code == 202 and resp.get("total") == 3 and len(resp.get("job_id", "")) == 16)
jid_all = resp["job_id"]
# deterministic concurrency guard: seed a fake running job (a real thread
# finishes faked work too fast to race reliably)
app._batch_jobs["seeded"] = {"job_id": "seeded", "action": "start", "names": [],
                             "total": 0, "done": 0, "results": [],
                             "status": "running", "created": time.time()}
code, resp = app.batch_create_job("stop", ["rbx2"])
check("second create while one running -> 409",
      code == 409 and "already running" in resp.get("error", ""))
del app._batch_jobs["seeded"]
# wait for the background thread to finish the (fast, faked) job
deadline = time.time() + 10
while time.time() < deadline:
    c, v = app.batch_get_job(jid_all)
    if v.get("status") == "done":
        break
    time.sleep(0.05)
check("background thread completes the job", v.get("status") == "done")
check("job covered all 3 instances", v.get("done") == 3 and len(v.get("results", [])) == 3)
check("stop results ok, docker stop called per local instance",
      all(r["ok"] for r in v["results"]) and
      CALLS["run"] == ["docker stop wd-rbx1", "docker stop wd-rbx2"])

print("== _run_batch_job deterministic (sync) ==")
reset()
# bypass the thread: seed a job manually, run worker synchronously
jid = "aabbccddeeff0011"
app._batch_jobs[jid] = {"job_id": jid, "action": "launch-roblox",
                        "names": ["rbx1", "rbx2", "srt1"], "total": 3,
                        "done": 0, "results": [], "status": "running",
                        "created": time.time()}
app._run_batch_job(jid)
c, v = app.batch_get_job(jid)
check("mixed batch: rbx1 ok, rbx2 fails (not running), srt1 ok via wing",
      v["status"] == "done" and v["done"] == 3 and
      [r["ok"] for r in v["results"]] == [True, False, True] and
      v["failed"] == ["rbx2"] and CALLS["launch"] == ["rbx1"])
check("one failure does not abort the batch (srt1 still attempted)",
      CALLS["wing"] == [("POST", "/api/agent/instances/srt1/launch-roblox", 30)])
# exception inside a single action never kills the batch
def boom_action(name, action):
    raise RuntimeError("kaput")
app.instance_single_action = boom_action
jid2 = "0011223344556677"
app._batch_jobs[jid2] = {"job_id": jid2, "action": "start",
                         "names": ["rbx1", "rbx2"], "total": 2,
                         "done": 0, "results": [], "status": "running",
                         "created": time.time()}
app._run_batch_job(jid2)
c, v = app.batch_get_job(jid2)
check("exception in one action -> recorded, batch still completes",
      v["status"] == "done" and v["done"] == 2 and
      all(not r["ok"] and "RuntimeError" in r["error"] for r in v["results"]))
# restore real single-action
del app.instance_single_action
reset()

print("== batch_get_job / batch_delete_job ==")
c, v = app.batch_get_job("deadbeef" * 2)
check("get unknown job -> 404", c == 404)
# deterministic: seed jobs directly (real threads finish faked work instantly)
app._batch_jobs["run1"] = {"job_id": "run1", "action": "start", "names": ["rbx1"],
                           "total": 1, "done": 0, "results": [],
                           "status": "running", "created": time.time()}
c, d = app.batch_delete_job("run1")
check("delete running job -> 409", c == 409 and "run1" in app._batch_jobs)
app._batch_jobs["done1"] = {"job_id": "done1", "action": "start", "names": ["rbx1"],
                            "total": 1, "done": 1,
                            "results": [{"name": "rbx1", "ok": True, "error": None}],
                            "status": "done", "created": time.time()}
c, d = app.batch_delete_job("done1")
check("delete finished job -> 200 and gone",
      c == 200 and d == {"ok": True} and app.batch_get_job("done1")[0] == 404)
c, d = app.batch_delete_job("deadbeef" * 2)
check("delete unknown job -> 404", c == 404)

print("== TTL sweep ==")
reset()
old = "0" * 16
app._batch_jobs[old] = {"job_id": old, "action": "start", "names": [],
                        "total": 0, "done": 0, "results": [],
                        "status": "done", "created": time.time() - 3600}
code, resp = app.batch_create_job("start", ["rbx1"])
check("stale job swept on next create", old not in app._batch_jobs)
# cleanup the running thread's job
deadline = time.time() + 10
while time.time() < deadline:
    if app.batch_get_job(resp["job_id"])[1].get("status") == "done":
        break
    time.sleep(0.05)
reset()

print("== route regexes ==")
check("POST /api/instances/batch not swallowed by (start|stop) route",
      re.fullmatch(r"/api/instances/([^/]+)/(start|stop)", "/api/instances/batch") is None)
check("GET batch job id matches", re.fullmatch(
    r"/api/instances/batch/([0-9a-f]+)", "/api/instances/batch/abc123def456").group(1) == "abc123def456")
check("DELETE /api/instances/batch/<id> matches batch-delete before generic",
      re.fullmatch(r"/api/instances/batch/([0-9a-f]+)", "/api/instances/batch/abc123") is not None)

print("== dashboard fleet-bar contract ==")
html = open(os.path.join(HERE, "..", "..", "manager", "static", "index.html")).read()
for bid in ("btnFleetStart", "btnFleetStop", "btnFleetRestart", "btnFleetRbx",
            "fleetModal", "fleetTitle", "fleetProg", "fleetStatus", "fleetFails",
            "btnFleetClose", "fleetbar"):
    check(f"dashboard has #{bid}", f'id="{bid}"' in html)
check("POSTs to /api/instances/batch", "'/api/instances/batch'" in html)
check("polls GET /api/instances/batch/<job_id>",
      "'/api/instances/batch/' + jid" in html)
check("stop/restart ask for confirmation", html.count("confirm(") >= 2)
check("progress bar width updated", "$('fleetProg').style.width" in html)
check("failed names listed", "s.failed.map(esc)" in html)
check("grid refreshes when job completes", "refresh();" in html)
check("polling stops on modal close", "clearInterval(fleetTimer)" in html)
check("modal uses .open class convention",
      "$('fleetModal').classList.add('open')" in html and
      "$('fleetModal').classList.remove('open')" in html)
check("fleetbar CSS present", ".fleetbar" in html and ".pbar" in html)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
