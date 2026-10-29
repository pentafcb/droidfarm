"""Unit tests for batch-job journal + restart recovery (manager/app.py +
dashboard fleet progress modals).

Run: python3 test_batch_journal.py

Gap fixed: fleet batch jobs lived only in memory — a manager restart
mid-job (watchdog v5 self-restart, deploy restart, crash) silently killed
the job; the dashboard poll 404'd and nobody knew which instances had
already been processed. Jobs are now journaled to disk after every
completed instance; _recover_batch_jobs() re-registers journaled jobs at
startup, marking still-"running" ones "interrupted".

Hermetic: load_instances/load_providers/run/container_status are
monkeypatched; the journal dir is a temp dir; no docker, no network.
"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

TMP = tempfile.mkdtemp(prefix="batchjournal-")
os.environ["JERUSALEM_DATA"] = os.path.join(TMP, "data")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("batchjournal_app", os.path.join(HERE, "..", "..", "manager", "app.py"))

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
    "rbx3": {"provider": "local"},
}
CREATED = []


def fake_load_instances():
    return dict(INSTANCES)


def fake_load_providers():
    return {}


def fake_action(name, action):
    return 200, {"ok": True}


def fake_action_half(name, action):
    # rbx2 always fails; used to verify partial progress journaling
    if name == "rbx2":
        return 500, {"error": "docker failed"}
    return 200, {"ok": True}


def fake_create_local(name, cpus, memory_mb, ip=None):
    CREATED.append(name)
    return 200, {"ok": True, "name": name}


def reset():
    CREATED.clear()
    app._batch_jobs.clear()
    app.load_instances = fake_load_instances
    app.load_providers = fake_load_providers
    app.instance_single_action = fake_action
    app.create_local_instance = fake_create_local
    # fresh hermetic journal dir per test
    jd = tempfile.mkdtemp(prefix="bj-")
    app.BATCH_JOURNAL_DIR = jd
    return jd


def journal_files(jd):
    return sorted(f for f in os.listdir(jd) if f.endswith(".json"))


def read_journal(jd, jid):
    with open(os.path.join(jd, jid + ".json")) as f:
        return json.load(f)


print("== journal path validation ==")
check("valid 16-hex -> path under journal dir",
      app._batch_journal_path("aabbccddeeff0011") ==
      os.path.join(app.BATCH_JOURNAL_DIR, "aabbccddeeff0011.json"))
for bad in ("../x", "../../etc/passwd", "XYZ", "aabbccddeeff001", "aabbccddeeff00112",
            "", None, "AABBCCDDEEFF0011", "aabbccddeeff00!1", "aabbccddeeff00 1"):
    check(f"malformed jid rejected: {bad!r}", app._batch_journal_path(bad) is None)

print("== journaling on create + per-instance progress ==")
jd = reset()
code, resp = app.batch_create_job("start", ["rbx1", "rbx2"])
check("batch_create_job -> 202", code == 202)
jid = resp["job_id"]
check("journal file written at creation", journal_files(jd) == [jid + ".json"])
st = os.stat(os.path.join(jd, jid + ".json"))
check("journal file is 600", stat.S_IMODE(st.st_mode) == 0o600)
j0 = read_journal(jd, jid)
# NOTE (race fix): the background worker is fast under fakes and may finish
# the job (status -> "done") before this read. The identity fields are
# immutable, so both lifecycle states are valid here.
check("journal content: job_id/action/names/status",
      j0["job_id"] == jid and j0["action"] == "start" and
      j0["names"] == ["rbx1", "rbx2"] and j0["status"] in ("running", "done"))
# wait for the background thread to finish the (fast, faked) job
deadline = time.time() + 10
while time.time() < deadline:
    c, v = app.batch_get_job(jid)
    if v.get("status") == "done":
        break
    time.sleep(0.05)
check("background thread completes", v.get("status") == "done")
jdone = read_journal(jd, jid)
check("journal on completion: status done, done==total==2",
      jdone["status"] == "done" and jdone["done"] == 2 and jdone["total"] == 2 and
      len(jdone["results"]) == 2)

print("== per-instance journaling (sync worker, mixed ok/fail) ==")
jd = reset()
app.instance_single_action = fake_action_half
sjid = "0011223344556677"
app._batch_jobs[sjid] = {"job_id": sjid, "action": "restart",
                         "names": ["rbx1", "rbx2", "rbx3"], "total": 3,
                         "done": 0, "results": [], "status": "running",
                         "created": time.time()}
app._journal_batch_job(app._batch_jobs[sjid])
app._run_batch_job(sjid)
jm = read_journal(jd, sjid)
check("journal accumulated all 3 results",
      jm["done"] == 3 and len(jm["results"]) == 3 and jm["status"] == "done")
check("journal records the failure honestly",
      [r["ok"] for r in jm["results"]] == [True, False, True] and
      jm["results"][1]["error"] == "docker failed")
check("journal has no password key anywhere",
      "password" not in json.dumps(jm).lower())

print("== delete + sweep remove journal files ==")
jd = reset()
c, r = app.batch_create_job("stop", ["rbx1"])
dj = r["job_id"]
deadline = time.time() + 10
while time.time() < deadline:
    cc, vv = app.batch_get_job(dj)
    if vv.get("status") == "done":
        break
    time.sleep(0.05)
check("journal exists before delete", os.path.exists(os.path.join(jd, dj + ".json")))
code, _ = app.batch_delete_job(dj)
check("delete -> 200 and journal file gone",
      code == 200 and not os.path.exists(os.path.join(jd, dj + ".json")))
# sweep: one expired job, one live job
old = "aaaaaaaaaaaaaaaa"
new = "bbbbbbbbbbbbbbbb"
app._batch_jobs[old] = {"job_id": old, "action": "start", "names": [],
                        "total": 0, "done": 0, "results": [],
                        "status": "done", "created": time.time() - 9999}
app._batch_jobs[new] = {"job_id": new, "action": "start", "names": [],
                        "total": 0, "done": 0, "results": [],
                        "status": "done", "created": time.time()}
app._journal_batch_job(app._batch_jobs[old])
app._journal_batch_job(app._batch_jobs[new])
app._sweep_batch_jobs()
check("sweep removed expired job + its journal, kept the live one",
      old not in app._batch_jobs and new in app._batch_jobs and
      not os.path.exists(os.path.join(jd, old + ".json")) and
      os.path.exists(os.path.join(jd, new + ".json")))

print("== _recover_batch_jobs: running -> interrupted, done stays done ==")
jd = reset()
rj = "cccccccccccccccc"
dj2 = "dddddddddddddddd"
app._batch_jobs[rj] = {"job_id": rj, "action": "roblox-login",
                       "names": ["rbx1", "rbx2", "rbx3"], "total": 3,
                       "done": 2,
                       "results": [{"name": "rbx1", "ok": True, "error": None},
                                   {"name": "rbx2", "ok": False, "error": "no credentials saved"}],
                       "status": "running", "created": time.time() - 60}
app._batch_jobs[dj2] = {"job_id": dj2, "action": "start", "names": ["rbx1"],
                        "total": 1, "done": 1,
                        "results": [{"name": "rbx1", "ok": True, "error": None}],
                        "status": "done", "created": time.time() - 60}
app._journal_batch_job(app._batch_jobs[rj])
app._journal_batch_job(app._batch_jobs[dj2])
# simulate a manager restart: drop the in-memory registry, recover from disk
app._batch_jobs.clear()
app._recover_batch_jobs()
check("both journaled jobs re-registered", rj in app._batch_jobs and dj2 in app._batch_jobs)
c, v = app.batch_get_job(rj)
check("interrupted job: status + reason in view",
      v["status"] == "interrupted" and v.get("interrupted_reason") == "manager restarted")
check("interrupted job keeps partial progress (done=2, rbx3 pending)",
      v["done"] == 2 and len(v["results"]) == 2 and
      [r["name"] for r in v["results"]] == ["rbx1", "rbx2"])
c, v = app.batch_get_job(dj2)
check("done job stays done, no interrupted_reason",
      v["status"] == "done" and "interrupted_reason" not in v)
check("interrupted job does NOT block a new job (guard is running-only)",
      app.batch_create_job("stop", ["rbx1"])[0] == 202)
app._batch_jobs.clear()
app._batch_jobs["eeeeeeeeeeeeeeee"] = {"job_id": "eeeeeeeeeeeeeeee", "action": "start",
                                       "names": [], "total": 0, "done": 0,
                                       "results": [], "status": "running",
                                       "created": time.time()}
check("genuinely running job still blocks a new one",
      app.batch_create_job("stop", ["rbx1"])[0] == 409)

print("== _recover_batch_jobs: hostile/odd journal files ==")
jd = reset()
os.makedirs(jd, exist_ok=True)
open(os.path.join(jd, "ffffffffffffffff.json"), "w").write("{not json")
open(os.path.join(jd, "gggggggggggggggg.json"), "w").write("{}")  # non-hex name
open(os.path.join(jd, "1111111111111111.json"), "w").write(
    json.dumps({"job_id": "2222222222222222"}))  # filename/job_id mismatch
open(os.path.join(jd, "3333333333333333.json"), "w").write(json.dumps([1, 2]))  # non-dict
open(os.path.join(jd, "4444444444444444.txt"), "w").write(
    json.dumps({"job_id": "4444444444444444", "status": "running"}))  # wrong ext
open(os.path.join(jd, "5555555555555555.json"), "w").write(
    json.dumps({"job_id": "5555555555555555"}))  # minimal: defaults fill in
app._recover_batch_jobs()
check("corrupt/non-hex/mismatch/non-dict/wrong-ext all skipped",
      not any(k in app._batch_jobs for k in
              ("ffffffffffffffff", "gggggggggggggggg", "1111111111111111",
               "3333333333333333", "4444444444444444")))
c, v = app.batch_get_job("5555555555555555")
check("minimal journal recovers with sane defaults",
      c == 200 and v["status"] == "interrupted" and v["done"] == 0 and
      v["total"] == 0 and v["results"] == [])
os.makedirs(os.path.join(jd, "6666666666666666.json"))  # a directory, not a file
app._recover_batch_jobs()  # must not raise
check("journal entry that is a directory is skipped, no raise",
      "6666666666666666" not in app._batch_jobs)

print("== journal failures never raise ==")
jd = reset()
blocker = os.path.join(jd, "blocker")
open(blocker, "w").write("x")
app.BATCH_JOURNAL_DIR = blocker  # makedirs on a file -> OSError, must be swallowed
try:
    app._journal_batch_job({"job_id": "7777777777777777", "status": "running"})
    app._unjournal_batch_job("7777777777777777")
    app._unjournal_batch_job("../escape")
    ok = True
except Exception:
    ok = False
check("journal/unjournal never raise on I/O failure or bad jid", ok)
app.BATCH_JOURNAL_DIR = jd
app._recover_batch_jobs()  # journal dir is now a file: listdir fails -> no-op
check("recovery with unreadable journal dir is a silent no-op",
      "7777777777777777" not in app._batch_jobs)

print("== fleet-create jobs journal too ==")
jd = reset()
code, resp = app.fleet_create_job("tst", 2, 2, 2048, "local")
check("fleet_create_job -> 202", code == 202)
fjid = resp["job_id"]
check("fleet-create journal written at creation",
      os.path.exists(os.path.join(jd, fjid + ".json")))
fj0 = read_journal(jd, fjid)
check("fleet-create journal: action/prefix/names, no password",
      fj0["action"] == "create-fleet" and fj0["prefix"] == "tst" and
      len(fj0["names"]) == 2 and "password" not in json.dumps(fj0).lower())
# wait for the background create thread, then isolate the sync run below
deadline = time.time() + 10
while time.time() < deadline:
    cc, vv = app.batch_get_job(fjid)
    if vv.get("status") == "done":
        break
    time.sleep(0.05)
check("background fleet-create thread completes", vv.get("status") == "done")
CREATED.clear()
# run the create worker synchronously against a seeded job
sjid2 = "8888888888888888"
app._batch_jobs[sjid2] = {"job_id": sjid2, "action": "create-fleet",
                          "names": ["tst1", "tst2"], "prefix": "tst",
                          "cpus": 2, "memory_mb": 2048, "provider": "local",
                          "total": 2, "done": 0, "results": [],
                          "status": "running", "created": time.time()}
app._run_fleet_create(sjid2)
fj = read_journal(jd, sjid2)
check("fleet-create worker journals per-instance progress",
      fj["done"] == 2 and fj["status"] == "done" and
      [r["name"] for r in fj["results"]] == ["tst1", "tst2"] and
      CREATED == ["tst1", "tst2"])
c, v = app.batch_get_job(sjid2)
check("create-fleet view still exposes planned names",
      c == 200 and v["names"] == ["tst1", "tst2"])

print("== dashboard contract ==")
with open(os.path.join(HERE, "..", "..", "manager", "static", "index.html")) as f:
    html = f.read()
check("fleet-action poll loop handles status 'interrupted'",
      "s.status === 'interrupted'" in html)
check("interrupted message is Spanish, names the restart",
      "Se interrumpi" in html and "servidor reiniciado" in html)
check("interrupted branch lists un-attempted names (Sin ejecutar / Sin crear)",
      "Sin ejecutar" in html and "Sin crear" in html)
check("poll survives a quick restart (~30 retries before giving up)",
      "FLEET_POLL_MAX_FAILS" in html and "pollFails" in html)
check("lost-job 404 gets a friendly Spanish message",
      "job not found" in html and "trabajo se perdi" in html)
check("main() recovers journaled jobs at startup",
      "_recover_batch_jobs()" in open(
          os.path.join(HERE, "..", "..", "manager", "app.py")).read())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
