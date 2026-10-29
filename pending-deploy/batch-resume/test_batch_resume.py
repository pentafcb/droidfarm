"""Unit tests for batch-job resume (manager/app.py + dashboard fleet modal).

Run: python3 test_batch_resume.py

Gap fixed: interrupted fleet jobs (manager restarted mid-job -> the
batch-journal marks them "interrupted" with partial progress) could only be
re-run in full — re-running "Restart all" double-restarted the instances
that had already completed. POST /api/instances/batch/<jid>/resume now
creates a NEW job covering only the never-attempted instances, for both
fleet actions (start/stop/restart/launch-roblox/roblox-login) and
create-fleet jobs. The dashboard's interrupted branch gained a "Reanudar"
button; the two duplicated progress-poll loops were unified into one
shared pollFleetJob().

Hermetic: load_instances/load_providers/instance_single_action/
create_local_instance are monkeypatched; the journal dir is a temp dir;
no docker, no network. The dashboard poller block is extracted and driven
in node with a stubbed DOM.
"""
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

TMP = tempfile.mkdtemp(prefix="batchresume-")
os.environ["JERUSALEM_DATA"] = os.path.join(TMP, "data")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("batchresume_app", os.path.join(HERE, "..", "..", "manager", "app.py"))

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
    "fc1": {"provider": "local"},
}
CALLED = []   # (name, action) seen by the fake batch worker
CREATED = []  # names seen by the fake fleet-create worker
BLOCK = threading.Event()


def fake_load_instances():
    return dict(INSTANCES)


def fake_load_providers():
    return {}


def fake_action(name, action):
    CALLED.append((name, action))
    return 200, {"ok": True}


def blocking_action(name, action):
    CALLED.append((name, action))
    BLOCK.wait(10)
    return 200, {"ok": True}


def fake_create_local(name, cpus, memory_mb, ip=None):
    CREATED.append(name)
    return 200, {"ok": True, "name": name}


def reset(blocking=False):
    CALLED.clear()
    CREATED.clear()
    BLOCK.clear()
    app._batch_jobs.clear()
    app.load_instances = fake_load_instances
    app.load_providers = fake_load_providers
    app.instance_single_action = blocking_action if blocking else fake_action
    app.create_local_instance = fake_create_local
    jd = tempfile.mkdtemp(prefix="br-")
    app.BATCH_JOURNAL_DIR = jd
    return jd


def make_job(jid, action, names, results, status="interrupted", **kw):
    job = {"job_id": jid, "action": action, "names": list(names),
           "total": len(names), "done": len(results),
           "results": list(results), "status": status,
           "created": time.time()}
    job.update(kw)
    app._batch_jobs[jid] = job
    return job


def wait_done(jid, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        job = app._batch_jobs.get(jid)
        if job and job.get("status") == "done":
            return job
        time.sleep(0.05)
    return app._batch_jobs.get(jid)


print("== resume validation ==")
jd = reset()
c, _ = app.batch_resume_job("deadbeefdeadbeef")
check("unknown job -> 404", c == 404)
make_job("a" * 16, "restart", ["rbx1"], [], status="done")
c, v = app.batch_resume_job("a" * 16)
check("done job -> 409 only-interrupted", c == 409 and "interrupted" in v["error"])
make_job("b" * 16, "restart", ["rbx1"], [], status="running")
c, v = app.batch_resume_job("b" * 16)
check("running job -> 409 only-interrupted", c == 409 and "interrupted" in v["error"])
app._batch_jobs["b" * 16]["status"] = "done"  # retire the fixture so later
# checks exercise their own validation, not the concurrency guard
make_job("c" * 16, "bogus-action", ["rbx1"], [])
c, v = app.batch_resume_job("c" * 16)
check("non-resumable action -> 409", c == 409 and "cannot be resumed" in v["error"])
make_job("d" * 16, "restart", ["rbx1"],
         [{"name": "rbx1", "ok": True, "error": None}])
c, v = app.batch_resume_job("d" * 16)
check("nothing pending (all attempted) -> 409", c == 409 and "nothing left" in v["error"])

print("== batch resume happy path ==")
jd = reset()
make_job("e" * 16, "restart", ["rbx1", "rbx2", "rbx3"],
         [{"name": "rbx1", "ok": True, "error": None},
          {"name": "rbx2", "ok": False, "error": "docker failed"}])
c, v = app.batch_resume_job("e" * 16)
check("resume -> 202 with new job_id", c == 202 and v.get("job_id") not in ("e" * 16,))
new = app._batch_jobs.get(v["job_id"])
check("new job covers only never-attempted names",
      new is not None and new["names"] == ["rbx3"] and new["action"] == "restart")
check("new job records resumed_from", new.get("resumed_from") == "e" * 16)
check("original job untouched (still interrupted)",
      app._batch_jobs["e" * 16]["status"] == "interrupted")
check("resumed_from persisted in the new journal",
      json.load(open(os.path.join(jd, v["job_id"] + ".json")))["resumed_from"] == "e" * 16)
done = wait_done(v["job_id"])
check("worker runs only the pending instance",
      done and done["status"] == "done" and CALLED == [("rbx3", "restart")])
check("no password material in the journal",
      "password" not in json.dumps(done["results"]).lower())

print("== one-job-at-a-time guard ==")
jd = reset(blocking=True)
c1, v1 = app.batch_create_job("restart", ["rbx1"])
check("first job starts (202)", c1 == 202)
make_job("f" * 16, "stop", ["rbx2", "rbx3"], [{"name": "rbx2", "ok": True, "error": None}])
c, v = app.batch_resume_job("f" * 16)
check("resume while another job runs -> 409", c == 409 and "already running" in v["error"])
BLOCK.set()
wait_done(v1["job_id"])
c, v = app.batch_resume_job("f" * 16)
check("resume works after the running job finishes", c == 202)
wait_done(v["job_id"])

print("== fleet-create resume ==")
jd = reset()
make_job("1" * 16, "create-fleet", ["fc1", "fc2", "fc3"],
         [{"name": "fc1", "ok": True, "error": None}],
         prefix="fc", cpus=2, memory_mb=2048, provider="local")
c, v = app.batch_resume_job("1" * 16)
check("create-fleet resume -> 202", c == 202)
new = app._batch_jobs.get(v["job_id"])
check("resumed create-fleet covers pending names with original params",
      new is not None and new["names"] == ["fc2", "fc3"] and
      new["action"] == "create-fleet" and new["cpus"] == 2 and
      new["memory_mb"] == 2048 and new["provider"] == "local" and
      new.get("resumed_from") == "1" * 16)
done = wait_done(v["job_id"])
check("create worker runs only the pending names",
      done and done["status"] == "done" and CREATED == ["fc2", "fc3"])

print("== deleted-instance filtering ==")
jd = reset()
make_job("2" * 16, "restart", ["rbx9"], [])  # rbx9 not in INSTANCES
c, v = app.batch_resume_job("2" * 16)
check("pending instance deleted meanwhile -> 409 nothing left",
      c == 409 and "nothing left" in v["error"])
make_job("3" * 16, "create-fleet", ["fc2", "fc9"], [],
         prefix="fc", cpus=2, memory_mb=2048, provider="local")
INSTANCES["fc9"] = {"provider": "local"}  # created outside the job meanwhile
c, v = app.batch_resume_job("3" * 16)
new = app._batch_jobs.get(v["job_id"]) if c == 202 else None
check("create-fleet resume skips names that exist now",
      c == 202 and new["names"] == ["fc2"])
del INSTANCES["fc9"]
wait_done(v["job_id"])

print("== hostile journal names are sanitized (shell-injection guard) ==")
jd = reset()
make_job("4" * 16, "restart",
         ["rbx1", "x;reboot", "$(id)", "../etc", "a" * 40, "", "rbx2|cat"],
         [])
c, v = app.batch_resume_job("4" * 16)
new = app._batch_jobs.get(v["job_id"]) if c == 202 else None
check("metachar names dropped, clean names kept",
      c == 202 and new["names"] == ["rbx1"])
done = wait_done(v["job_id"])
check("worker never sees the hostile names",
      done and [n for n, _ in CALLED] == ["rbx1"])
make_job("5" * 16, "create-fleet", ["x;reboot"], [],
         prefix="x", cpus=2, memory_mb=2048, provider="local")
c, v = app.batch_resume_job("5" * 16)
check("only-hostile pending -> 409 nothing left", c == 409)
check("no worker thread started for the refused resume", CREATED == [])

print("== garbage job dicts never raise ==")
jd = reset()
make_job("6" * 16, "restart", ["rbx1", "rbx2"], [])
app._batch_jobs["6" * 16]["results"] = "garbage-string"
c, v = app.batch_resume_job("6" * 16)
check("non-list results tolerated (treated as none attempted)", c == 202)
wait_done(v["job_id"])
app._batch_jobs["7" * 16] = {"job_id": "7" * 16, "action": "restart",
                             "names": "not-a-list", "status": "interrupted",
                             "results": [], "created": time.time()}
c, v = app.batch_resume_job("7" * 16)
check("non-list names -> 409, no raise", c == 409)
app._batch_jobs["8" * 16] = "not-a-dict"
c, v = app.batch_resume_job("8" * 16)
check("non-dict job -> 500, no raise", c == 500)
c, v = app.batch_resume_job(None)
check("None jid -> 404, no raise", c == 404)

print("== route wiring ==")
src = inspect.getsource(app.Handler.do_POST)
gate_at = src.index("if not self._authed():")
route_at = src.index("/api/instances/batch/([0-9a-f]+)/resume")
check("resume route exists in do_POST after the auth gate", gate_at < route_at)
check("route regex only matches 16-hex job ids",
      re.search(r"batch/\(\[0-9a-f\]\+\)/resume", src) is not None)
check("resume delegates to batch_resume_job",
      "batch_resume_job(m.group(1))" in src)

print("== dashboard contract ==")
html = open(os.path.join(HERE, "..", "..", "manager", "static", "index.html")).read()
check("shared poller constant: 30-failure tolerance",
      "FLEET_POLL_MAX_FAILS = 30" in html)
check("both wording sets present",
      "FLEET_BATCH_WORDS" in html and "FLEET_CREATE_WORDS" in html)
check("batch call site uses batch words", "FLEET_BATCH_WORDS, j.total" in html)
check("fleet-create call site uses create words",
      "FLEET_CREATE_WORDS, j.total" in html)
check("old duplicated loops are gone",
      "fleetPollFails" not in html and "fleetCreatePollFails" not in html)
check("pollFleetJob + resumeFleetJob defined",
      "function pollFleetJob(" in html and "function resumeFleetJob(" in html)
check("resume posts to the /resume route",
      "'/resume'" in html and "batch/' + origJid + '/resume" in html)
check("Reanudar button with pending count",
      "Reanudar (" in html and "pendientes" in html)
check("resumed job title marked (reanudado)", "(reanudado)" in html)
check("interrupted Spanish strings preserved",
      "Se interrumpi" in html and "servidor reiniciado" in html and
      "Sin ejecutar" in html and "Sin crear" in html)
check("pending names escaped", "pending.map(esc)" in html)
m = re.search(r"// \[batch-resume: poller\](.*)// \[/batch-resume: poller\]",
              html, re.S)
check("poller block extractable for the node test", m is not None)
block = m.group(1) if m else ""
emoji = [c for c in block
         if (0x1F300 <= ord(c) <= 0x1FAFF) or (0x2600 <= ord(c) <= 0x27BF)
         or ord(c) == 0xFE0F]
check("no emojis in the new dashboard block", not emoji)

print("== node-driven poller state machine ==")
driver = r"""
const els = {};
function makeEl(id){ return {id, textContent:'', _html:'', style:{},
  classList:{add(){},remove(){},toggle(){}},
  set innerHTML(v){ this._html = v;
    if (v.includes('btnFleetResume') && !els['btnFleetResume']) els['btnFleetResume']=makeEl('btnFleetResume'); },
  get innerHTML(){ return this._html; }, onclick:null}; }
function $(id){ return els[id] || (els[id] = makeEl(id)); }
const esc = s => String(s);
let refreshCalls = 0; function refresh(){ refreshCalls++; }
let fleetTimer = null; let captured = null;
function setInterval(fn, ms){ captured = fn; return 7; }
function clearInterval(){ captured = null; }
const alerts = []; function alert(m){ alerts.push(m); }
let apiCalls = [];
async function api(url, opts){
  apiCalls.push([url, (opts||{}).method || 'GET']);
  if (url.endsWith('/resume')){
    if ((opts||{}).method !== 'POST') throw new Error('resume must be POST');
    return {job_id:'newjob1', total:2};
  }
  const jid = url.split('/').pop();
  return SCENARIO(jid);
}
let SCENARIO = jid => ({status:'running', done:0, total:2});
__BLOCK__
(async () =>{
  const out = [];
  const ok = (n,c) => out.push((c?'ok  ':'FAIL ')+n);
  // A: interrupted -> Reanudar -> resume POST -> new job polls -> done
  SCENARIO = jid => jid === 'orig1'
    ? {status:'interrupted', done:2, total:3, job_id:'orig1',
       names:['a','b','c'], results:[{name:'a',ok:true},{name:'b',ok:false}]}
    : {status:'done', done:2, total:2, job_id:'newjob1', failed:[]};
  pollFleetJob('orig1', ' Flota: X', FLEET_BATCH_WORDS, 3);
  ok('A title set', $('fleetTitle').textContent === ' Flota: X');
  await captured();
  ok('A interrupted message (Spanish, restart named)',
     $('fleetStatus').textContent.includes('Se interrumpi') &&
     $('fleetStatus').textContent.includes('servidor reiniciado') &&
     $('fleetStatus').textContent.includes('completadas'));
  ok('A pending list shows Sin ejecutar + the pending name',
     $('fleetFails').innerHTML.includes('Sin ejecutar: c'));
  ok('A Reanudar button rendered with count',
     $('fleetFails').innerHTML.includes('btnFleetResume') &&
     $('fleetFails').innerHTML.includes('Reanudar (1 pendientes)'));
  ok('A poll loop stopped on interrupted', captured === null);
  await els['btnFleetResume'].onclick();
  ok('A resume POSTed to /resume',
     apiCalls.some(([u,m]) => u.endsWith('/resume') && m === 'POST'));
  ok('A new poll runs under the resumed title',
     $('fleetTitle').textContent === ' Flota: X (reanudado)');
  await captured();
  ok('A resumed job reaching done shows Listo',
     $('fleetStatus').textContent.includes('Listo'));
  ok('A grid refreshes on done', refreshCalls > 0);
  // B: ~30 dropped polls before the job is declared lost
  apiCalls = [];
  SCENARIO = () => { throw new Error('boom'); };
  pollFleetJob('lost1', ' Flota: Y', FLEET_BATCH_WORDS, 2);
  for (let i=0;i<29;i++) await captured();
  ok('B 29 failures: still polling, no lost message',
     captured !== null && !$('fleetStatus').textContent.includes('Error'));
  await captured();
  ok('B 30th failure: friendly error shown',
     $('fleetStatus').textContent.includes('Error: boom'));
  SCENARIO = () => { throw new Error('job not found'); };
  pollFleetJob('lost2', ' Flota: Z', FLEET_BATCH_WORDS, 2);
  for (let i=0;i<30;i++) await captured();
  ok('B 404 maps to the Spanish lost-job message',
     $('fleetStatus').textContent.includes('trabajo se perdi'));
  // C: create wording + failed list
  SCENARIO = () => ({status:'done', done:2, total:3, failed:['n2']});
  pollFleetJob('c1', ' Flota: Crear 3 instancias', FLEET_CREATE_WORDS, 3);
  await captured();
  ok('C failed names listed under Fallaron',
     $('fleetFails').innerHTML.includes('Fallaron') &&
     $('fleetFails').innerHTML.includes('n2'));
  SCENARIO = () => ({status:'interrupted', done:1, total:3, job_id:'c1',
    names:['n1','n2','n3'], results:[{name:'n1',ok:true}]});
  pollFleetJob('c1', ' Flota: Crear 3 instancias', FLEET_CREATE_WORDS, 3);
  await captured();
  ok('C create wording: Sin crear + creadas',
     $('fleetFails').innerHTML.includes('Sin crear') &&
     $('fleetStatus').textContent.includes('creadas'));
  const fails = out.filter(l => l.startsWith('FAIL'));
  console.log(out.join('\n'));
  console.log(fails.length + ' node-failures');
  process.exit(fails.length ? 1 : 0);
})();
""".replace("__BLOCK__", block)
open("/tmp/batchresume_driver.js", "w").write(driver)
r = subprocess.run(["node", "/tmp/batchresume_driver.js"], capture_output=True,
                   text=True, timeout=30)
print(r.stdout.strip())
if r.returncode != 0:
    print(r.stderr.strip()[-2000:])
n_ok = len(re.findall(r"^ok  ", r.stdout, re.M))
n_bad = len(re.findall(r"^FAIL ", r.stdout, re.M))
PASS += n_ok
FAIL += n_bad
check("node driver exited clean with its summary line",
      r.returncode == 0 and "node-failures" in r.stdout)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
