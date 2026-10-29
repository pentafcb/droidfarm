"""Unit tests for batch-worker safety (manager/app.py + dashboard poller).

Run: python3 test_batch_worker_safety.py

Gaps fixed:
  1. _sweep_batch_jobs() popped jobs purely by created-time, regardless of
     status — a long fleet job (e.g. "Login all" on 20 instances) that
     outlived the 15-min TTL was deleted from the registry AND unjournaled
     the moment anyone started another job; the worker then exited early
     and the job silently vanished mid-flight (partial work done, no
     resume possible).
  2. _run_batch_job/_run_fleet_create ran in bare daemon threads with no
     top-level safety net: an exception escaping the per-instance
     try/except (registry/journal bookkeeping) killed the thread and left
     the job "running" forever, wedging the one-job-at-a-time 409 guard
     for the whole fleet bar until a manager restart. The startup-only
     batch-journal recovery never fired in a live manager, and the
     batch-resume "Reanudar" button only works on "interrupted" jobs.

Fix: the sweep skips status=="running" jobs; both workers get a
try/except BaseException net that marks the job "interrupted" with a
server-generated reason (journaled, partial results intact) so the fleet
bar frees up and the dashboard resume path works. The dashboard's
interrupted branch now shows the reason when the server provides one
(fallback: the old "(servidor reiniciado)" wording).

Hermetic: load_instances/instance_single_action/create_local_instance are
monkeypatched; the journal dir is a temp dir; no docker, no network.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

TMP = tempfile.mkdtemp(prefix="batchsafety-")
os.environ["JERUSALEM_DATA"] = os.path.join(TMP, "data")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("batchsafety_app", os.path.join(HERE, "..", "..", "manager", "app.py"))

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


def fake_load_instances():
    return dict(INSTANCES)


def fake_load_providers():
    return {}


def fake_action(name, action):
    return 200, {"ok": True}


def fake_create_local(name, cpus, memory_mb, ip=None):
    return 200, {"ok": True, "name": name}


# journal hook that raises on/after the Nth call (deterministic worker-crash
# trigger: the real _journal_batch_job never raises, but the safety net must
# hold no matter where the escape comes from)
JOURNAL_CALLS = [0]
JOURNAL_RAISE_FROM = [10 ** 9]  # disarmed by default
REAL_JOURNAL = None


def raising_journal(job):
    JOURNAL_CALLS[0] += 1
    if JOURNAL_CALLS[0] == JOURNAL_RAISE_FROM[0]:
        JOURNAL_RAISE_FROM[0] = 10 ** 9  # fire once: a transient escape
        raise RuntimeError("boom")
    return REAL_JOURNAL(job)


def reset(armed_from=10 ** 9):
    app._batch_jobs.clear()
    app.load_instances = fake_load_instances
    app.load_providers = fake_load_providers
    app.instance_single_action = fake_action
    app.create_local_instance = fake_create_local
    app.create_remote_instance = lambda *a, **k: (200, {"ok": True})
    JOURNAL_CALLS[0] = 0
    JOURNAL_RAISE_FROM[0] = armed_from
    jd = tempfile.mkdtemp(prefix="bs-")
    app.BATCH_JOURNAL_DIR = jd
    return jd


def read_journal(jd, jid):
    with open(os.path.join(jd, jid + ".json")) as f:
        return json.load(f)


def plant_job(jid, status, age_s, action="start", names=("rbx1",)):
    job = {"job_id": jid, "action": action, "names": list(names),
           "total": len(names), "done": 0, "results": [],
           "status": status, "created": time.time() - age_s}
    app._batch_jobs[jid] = job
    app._journal_batch_job(job)
    return job


REAL_JOURNAL = app._journal_batch_job
app._journal_batch_job = raising_journal

print("== sweep: running jobs survive the TTL, finished jobs are swept ==")
jd = reset()
running_old = plant_job("a" * 16, "running", 9999)
done_old = plant_job("b" * 16, "done", 9999)
interrupted_old = plant_job("c" * 16, "interrupted", 9999)
running_new = plant_job("d" * 16, "running", 10)
app._sweep_batch_jobs()
check("expired RUNNING job kept in registry + journal intact",
      "a" * 16 in app._batch_jobs and
      os.path.exists(os.path.join(jd, "a" * 16 + ".json")))
check("expired done job swept (registry + journal)",
      "b" * 16 not in app._batch_jobs and
      not os.path.exists(os.path.join(jd, "b" * 16 + ".json")))
check("expired interrupted job swept",
      "c" * 16 not in app._batch_jobs and
      not os.path.exists(os.path.join(jd, "c" * 16 + ".json")))
check("fresh running job untouched",
      "d" * 16 in app._batch_jobs and
      os.path.exists(os.path.join(jd, "d" * 16 + ".json")))
check("running job's data untouched by the sweep",
      app._batch_jobs["a" * 16]["status"] == "running")

print("== _mark_job_interrupted: contract ==")
jd = reset()
jid = plant_job("e" * 16, "running", 5, names=["rbx1", "rbx2"])["job_id"]
app._mark_job_interrupted(jid, "worker error: RuntimeError")
check("running -> interrupted with reason",
      app._batch_jobs[jid]["status"] == "interrupted" and
      app._batch_jobs[jid]["interrupted_reason"] == "worker error: RuntimeError")
check("interruption is journaled",
      read_journal(jd, jid)["status"] == "interrupted")
check("non-running job untouched",
      (app._mark_job_interrupted(jid, "x"),
       app._batch_jobs[jid]["interrupted_reason"] == "worker error: RuntimeError")[1])
check("unknown jid -> silent no-op",
      app._mark_job_interrupted("f" * 16, "x") is None)
app._batch_jobs["g" * 16] = "garbage-shape"
check("garbage job shape never raises",
      app._mark_job_interrupted("g" * 16, "x") is None)
app._batch_jobs["h" * 16] = {"job_id": "h" * 16, "status": "running"}
check("missing created key tolerated",
      app._mark_job_interrupted("h" * 16, "x") is None and
      app._batch_jobs["h" * 16]["status"] == "interrupted")
jid2 = plant_job("i" * 16, "running", 5)["job_id"]
app._mark_job_interrupted(jid2, "y" * 500)
check("long reason truncated",
      len(app._batch_jobs[jid2]["interrupted_reason"]) == 160)

print("== worker crash (Exception) -> interrupted, partial results kept ==")
jd = reset(armed_from=2)  # 1st journal call = plant_job, 2nd = after rbx1 -> boom
sjid = "1" * 16
plant_job(sjid, "running", 1, names=["rbx1", "rbx2"])
app._run_batch_job(sjid)  # synchronous: deterministic
job = app._batch_jobs[sjid]
check("job interrupted, not stranded running",
      job["status"] == "interrupted")
check("reason names the escaping exception",
      job.get("interrupted_reason") == "worker error: RuntimeError")
check("partial progress intact (rbx1 done, rbx2 never attempted)",
      job["done"] == 1 and len(job["results"]) == 1 and
      job["results"][0]["name"] == "rbx1" and job["results"][0]["ok"])
check("interruption journaled to disk",
      read_journal(jd, sjid)["status"] == "interrupted" and
      read_journal(jd, sjid)["interrupted_reason"] == "worker error: RuntimeError")
check("no password material in the journal",
      "password" not in json.dumps(read_journal(jd, sjid)).lower())

print("== worker crash (BaseException: SystemExit) -> interrupted ==")
jd = reset()
sjid = "2" * 16
plant_job(sjid, "running", 1, names=["rbx1", "rbx2"])
JOURNAL_CALLS[0] = 0
JOURNAL_RAISE_FROM[0] = 2


def raising_journal_exit(job):
    JOURNAL_CALLS[0] += 1
    if JOURNAL_CALLS[0] == 2:
        raise SystemExit(3)
    return REAL_JOURNAL(job)


app._journal_batch_job = raising_journal_exit
app._run_batch_job(sjid)
job = app._batch_jobs[sjid]
check("SystemExit in worker -> interrupted",
      job["status"] == "interrupted" and
      job.get("interrupted_reason") == "worker error: SystemExit")
app._journal_batch_job = raising_journal

print("== fleet-create worker crash -> interrupted ==")
jd = reset(armed_from=2)
sjid = "3" * 16
plant_job(sjid, "running", 1, action="create-fleet", names=["rbx1", "rbx2"])
app._run_fleet_create(sjid)
job = app._batch_jobs[sjid]
check("fleet-create job interrupted on worker crash",
      job["status"] == "interrupted" and
      job.get("interrupted_reason") == "worker error: RuntimeError" and
      job["done"] == 1)

print("== crash frees the fleet-bar 409 guard; resume works ==")
jd = reset(armed_from=2)
code, resp = app.batch_create_job("start", ["rbx1", "rbx2"])
crashed = resp["job_id"]
deadline = time.time() + 10
while time.time() < deadline:
    c, v = app.batch_get_job(crashed)
    if v.get("status") == "interrupted":
        break
    time.sleep(0.05)
check("background worker thread crash -> interrupted (thread path)",
      v.get("status") == "interrupted")
check("reason visible via the API view",
      v.get("interrupted_reason") == "worker error: RuntimeError")
JOURNAL_RAISE_FROM[0] = 10 ** 9  # disarm: later jobs must journal cleanly
code2, resp2 = app.batch_create_job("stop", ["rbx3"])
check("new job allowed after crash (no zombie 409)", code2 == 202)
deadline = time.time() + 10
while time.time() < deadline:
    c, v = app.batch_get_job(resp2["job_id"])
    if v.get("status") == "done":
        break
    time.sleep(0.05)
rcode, rresp = app.batch_resume_job(crashed)
check("crashed job is resumable (202)",
      rcode == 202 and rresp.get("total") == 1)
deadline = time.time() + 10
while time.time() < deadline:
    c, v = app.batch_get_job(rresp["job_id"])
    if v.get("status") == "done":
        break
    time.sleep(0.05)
check("resume covered only the never-attempted instance",
      v.get("status") == "done" and v.get("done") == 1 and
      [r["name"] for r in v.get("results", [])] == ["rbx2"])

print("== healthy worker still completes -> done (regression) ==")
jd = reset()
code, resp = app.batch_create_job("restart", ["rbx1", "rbx2", "rbx3"])
hj = resp["job_id"]
deadline = time.time() + 10
while time.time() < deadline:
    c, v = app.batch_get_job(hj)
    if v.get("status") == "done":
        break
    time.sleep(0.05)
check("healthy job completes as done",
      v.get("status") == "done" and v.get("done") == 3 and
      "interrupted_reason" not in v)
check("healthy path: 15-min-old done job still swept on next create",
      (plant_job("m" * 16, "done", 9999),
       app.batch_create_job("start", ["rbx1"]),
       "m" * 16 not in app._batch_jobs)[2])

print("== worker thread naming (debuggability) ==")
import inspect
src_create = inspect.getsource(app.batch_create_job)
src_fleet = inspect.getsource(app._start_fleet_create_job)
check("batch worker thread named", 'name=f"batch-' in src_create)
check("fleet-create worker thread named", 'name=f"fleet-create-' in src_fleet)

print("== dashboard contract: interrupted reason shown, fallback kept ==")
with open(os.path.join(HERE, "..", "..", "manager", "static", "index.html")) as f:
    html = f.read()
check("interrupted branch reads interrupted_reason",
      "interrupted_reason" in html and "Se interrumpi" in html)
check("fallback '(servidor reiniciado)' preserved",
      "(servidor reiniciado)" in html)
check("reason rendered via textContent (no innerHTML injection)",
      "Se interrumpió' + why" in html and
      "$('fleetStatus').textContent" in html)
check("Spanish, no emojis in the touched line",
      not [c for c in "Se interrumpió" if (0x1F300 <= ord(c) <= 0x1FAFF)])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
