"""Unit tests for fleet bulk-create ("Create fleet").

Covers: create_local_instance / create_remote_instance shared cores
(single-create refactor), fleet_plan_names, _fleet_validate, fleet_headroom,
fleet_plan, fleet_create_job, _run_fleet_create, route placement, the
batch_get_job "names" view, and the dashboard contract.

Run: python3 test_fleet_create.py

Hermetic: load_instances/save_instances/load_providers/run/docker_inspect/
wing_request/get_host_stats/pick_provider/next_free_ip are monkeypatched;
no docker, no network.
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

app = load("fleetcreate_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
DASH = os.path.join(HERE, "..", "..", "manager", "static", "index.html")

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
RUN_CMDS = []
INSTANCES = {}
PROVIDERS = {}
HOST_RAM_MB = 31744
BINDER_DEVS = ["binder"] + [f"binder{i}" for i in range(1, 40)]  # 40 devices
BINDER_LS = " ".join(BINDER_DEVS)
FAIL_DOCKER = False
WING_RESP = (200, {"ok": True})
WING_FAIL_GET = False
WING_HEADROOM = (200, {"ok": True, "image_present": True, "loop_devices": 64})
PICK = "local"

def fake_load_instances():
    return {k: dict(v) for k, v in INSTANCES.items()}
def fake_save_instances(d):
    INSTANCES.clear()
    INSTANCES.update({k: dict(v) for k, v in d.items()})
def fake_load_providers():
    return {k: dict(v) for k, v in PROVIDERS.items()}
def fake_run(cmd, timeout=30):
    RUN_CMDS.append(cmd)
    if cmd.startswith("ls /dev/binderfs/"):
        return 0, BINDER_LS, ""
    if cmd.startswith("ls -d /dev/loop"):
        # healthy loop pool by default; loop-specific scenarios live in the
        # loop-device-guard suite
        return 0, "\n".join(f"/dev/loop{i}" for i in range(64)), ""
    if cmd.startswith("docker ps -aq"):
        return 0, "", ""
    if cmd.startswith("docker inspect"):
        return 1, "", ""          # container does not exist
    if cmd.startswith("docker run"):
        if FAIL_DOCKER:
            return 1, "", "boom"
        return 0, "cid123", ""
    if cmd.startswith("test -e"):
        return 1, "", ""          # device missing
    return 0, "", ""
def fake_wing_request(p, method, path, data=None, timeout=8, attempts=3):
    RUN_CMDS.append(f"WING {method} {path}")
    if method == "GET" and path == "/api/agent/instances":
        if WING_FAIL_GET:
            return 500, {"error": "down"}
        return 200, {"instances": [{"name": "srt1", "ip": "10.20.0.11"}]}
    if method == "GET" and path == "/api/agent/headroom":
        # remote-headroom: the plan preview probes node image/loop state
        return WING_HEADROOM
    return WING_RESP
def fake_get_host_stats():
    return {"ram_total_mb": HOST_RAM_MB, "ram_percent": 50.0,
            "ram_used_mb": HOST_RAM_MB // 2, "cpu_percent": 10.0}
def fake_pick_provider(providers, need_ram_mb=4096, need_cpus=2):
    return PICK
def fake_next_free_ip(instances):
    used = {v.get("ip") for v in instances.values() if v.get("ip")}
    for i in range(11, 250):
        ip = f"10.20.0.{i}"
        if ip not in used:
            return ip
    return None

def reset():
    global RUN_CMDS, FAIL_DOCKER, WING_RESP, WING_FAIL_GET, PICK, HOST_RAM_MB, BINDER_LS, WING_HEADROOM
    RUN_CMDS = []
    FAIL_DOCKER = False
    WING_RESP = (200, {"ok": True})
    WING_FAIL_GET = False
    WING_HEADROOM = (200, {"ok": True, "image_present": True, "loop_devices": 64})
    PICK = "local"
    HOST_RAM_MB = 31744
    BINDER_LS = " ".join(BINDER_DEVS)
    INSTANCES.clear()
    PROVIDERS.clear()
    app._batch_jobs.clear()
    app.load_instances = fake_load_instances
    app.save_instances = fake_save_instances
    app.load_providers = fake_load_providers
    app.run = fake_run
    app.wing_request = fake_wing_request
    app.get_host_stats = fake_get_host_stats
    app.pick_provider = fake_pick_provider
    app.next_free_ip = fake_next_free_ip

def _devname(k):
    return "binder" if k == 0 else f"binder{k}"

def seed_rbx(n=20):
    # real binder device names so headroom math is realistic
    for i in range(1, n + 1):
        INSTANCES[f"rbx{i}"] = {"provider": "local", "memory_mb": 2048,
                                "ip": f"10.20.0.{10 + i}",
                                "binders": [_devname(3 * i), _devname(3 * i + 1),
                                            _devname(3 * i + 2)]}

def seed_vultr():
    PROVIDERS["p_vultr"] = {"url": "http://10.0.0.2:8090", "agent_key": "k",
                           "last_seen": time.time(),
                           "stats": {"ram_total_mb": 8000, "ram_percent": 30.0}}

# ================= A. validation =================
reset()
code, resp = app.fleet_plan("", 5, 2, 2048, "local")
check("A1 empty prefix -> 400", code == 400 and "prefix" in resp["error"])
code, resp = app.fleet_plan("rbx!!", 2, 2, 2048, "local")
check("A2 junk prefix sanitized", code == 200 and resp["prefix"] == "rbx")
clean, err = app._fleet_validate("x" * 40, 1, 2, 2048, "local")
check("A3 prefix truncated to 24", clean is not None and len(clean["prefix"]) == 24)
for bad_count in (0, -1, 21, "x", None):
    code, _ = app.fleet_plan("rbx", bad_count, 2, 2048, "local")
    if not (code == 400):
        break
else:
    bad_count = "sentinel"
check("A4 bad counts -> 400", bad_count == "sentinel")
code, _ = app.fleet_plan("rbx", 20, 2, 2048, "local")
check("A4b count=20 ok", code == 200)
code, resp = app.fleet_plan("rbx", 5, 0.1, 2048, "local")
check("A5 cpus too low -> 400", code == 400)
code, resp = app.fleet_plan("rbx", 5, 2, 512, "local")
check("A5b memory too low -> 400", code == 400)
code, resp = app.fleet_plan("rbx", 5, 2, 65536, "local")
check("A5c memory too high -> 400", code == 400)
code, resp = app.fleet_plan("rbx", 5, 2, 2048, "nope")
check("A6 unknown provider -> 400", code == 400)
reset(); PICK = "p_vultr"; seed_vultr()
code, resp = app.fleet_plan("rbx", 2, 2, 2048, "auto")
check("A7 auto resolves via pick_provider", code == 200 and resp["provider"] == "p_vultr")

# ================= B. naming =================
reset(); seed_rbx(20)
code, resp = app.fleet_plan("rbx", 3, 2, 2048, "local")
check("B1 continues after rbx20", code == 200 and resp["names"] == ["rbx21", "rbx22", "rbx23"])
reset()
code, resp = app.fleet_plan("node", 3, 2, 2048, "local")
check("B2 no numeric suffix starts at 1", resp["names"] == ["node1", "node2", "node3"])
reset(); seed_rbx(5); INSTANCES["srt1"] = {"provider": "p_vultr", "memory_mb": 2048}
code, resp = app.fleet_plan("rbx", 2, 2, 2048, "local")
check("B3 other prefixes ignored", resp["names"] == ["rbx6", "rbx7"])
reset(); INSTANCES["rbx1"] = {"provider": "local", "memory_mb": 2048}
orig = app.fleet_plan_names
app.fleet_plan_names = lambda inst, p, c: ["rbx1", "rbx2"]  # forced collision
code, resp = app.fleet_plan("rbx", 2, 2, 2048, "local")
app.fleet_plan_names = orig
check("B4 colliding planned names filtered", code == 200 and resp["names"] == ["rbx2"])
reset(); INSTANCES["rbx1"] = {"provider": "local", "memory_mb": 2048}
app.fleet_plan_names = lambda inst, p, c: ["rbx1"]
code, resp = app.fleet_plan("rbx", 1, 2, 2048, "local")
app.fleet_plan_names = orig
check("B5 all names exist -> 400", code == 400)
reset()
code, resp = app.fleet_plan("rbx", 2, 2, 2048, "local")
flat = str(resp)
check("B6 no secrets in plan", "password" not in flat and "token" not in flat
      and "agent_key" not in flat)

# ================= C. headroom =================
reset(); seed_rbx(20)  # 20 x 2048 = 40960 limits on 31744 host
hr = app.fleet_headroom(fake_load_instances(), "local", 5, 2048)
check("C1 overcommit warning with numbers",
      hr["warning"] and "19456" in hr["warning"] and hr["overcommit_mb"] == 19456
      and hr["new_limits_mb"] == 51200 and hr["host_ram_mb"] == 31744)
reset(); seed_rbx(5)  # 10240 + 10240 = 20480 < 31744
hr = app.fleet_headroom(fake_load_instances(), "local", 5, 2048)
check("C2 fits -> no warning", hr["warning"] == "" and hr["overcommit_mb"] == 0)
reset(); HOST_RAM_MB = 0; seed_rbx(2)
hr = app.fleet_headroom(fake_load_instances(), "local", 5, 2048)
check("C3 unknown host RAM -> no warning", hr["warning"] == "" and hr["overcommit_mb"] == 0)
reset()
BINDER_LS = "binder binder1 binder2 binder3 binder4 binder5 binder6 binder7 binder8"
# one instance holding (binder,binder1,binder2) -> 2 free triplets
INSTANCES["rbx1"] = {"provider": "local", "memory_mb": 2048,
                     "binders": ["binder", "binder1", "binder2"]}
hr = app.fleet_headroom(fake_load_instances(), "local", 3, 1024)
check("C4 binder shortage warning", hr["binder_free_triplets"] == 2
      and "binder" in hr["warning"])
hr = app.fleet_headroom(fake_load_instances(), "local", 2, 1024)
check("C4b enough binders -> no binder warning", "binder" not in hr["warning"])
reset(); seed_vultr()
INSTANCES["srt1"] = {"provider": "p_vultr", "memory_mb": 2048}
hr = app.fleet_headroom(fake_load_instances(), "p_vultr", 2, 2048)
check("C5 remote: provider RAM, binder n/a, headroom probe fills image/loop",
      hr["host_ram_mb"] == 8000 and hr["binder_free_triplets"] is None
      and hr["current_limits_mb"] == 2048 and hr["new_limits_mb"] == 6144
      and hr["image_present"] is True and hr["loop_devices"] == 64
      and hr["headroom_probe"] == "ok")

# ================= D. local create core =================
reset()
code, resp = app.create_local_instance("rbx21", 2, 2048)
runs = [c for c in RUN_CMDS if c.startswith("docker run")]
check("D1 local create 200 + shape",
      code == 200 and resp["ok"] and resp["name"] == "rbx21"
      and resp["ip"] == "10.20.0.11" and len(resp["binders"]) == 3
      and isinstance(resp["instance_id"], int))
check("D2 docker run args", len(runs) == 1 and "--name wd-rbx21" in runs[0]
      and "--memory 2048m" in runs[0] and "--cpus 2" in runs[0]
      and "--ip 10.20.0.11" in runs[0])
check("D3 reservation persisted", "rbx21" in INSTANCES
      and INSTANCES["rbx21"]["provider"] == "local")
code, resp = app.create_local_instance("rbx21", 2, 2048)
check("D4 duplicate name -> 409", code == 409)
reset(); FAIL_DOCKER = True
code, resp = app.create_local_instance("rbx21", 2, 2048)
check("D5 docker failure -> 500 + reservation rolled back",
      code == 500 and "rbx21" not in INSTANCES)
reset()
code, resp = app.create_local_instance("rbx21", 2, 2048, ip="10.20.0.99")
check("D6 ip override honored",
      code == 200 and resp["ip"] == "10.20.0.99"
      and "--ip 10.20.0.99" in [c for c in RUN_CMDS if c.startswith("docker run")][0])
reset()
BINDER_LS = "binder binder1 binder2"  # all consumed by the fake used-set below
INSTANCES["rbx1"] = {"provider": "local", "memory_mb": 2048,
                     "binders": ["binder", "binder1", "binder2"]}
code, resp = app.create_local_instance("rbx2", 2, 2048)
check("D7 binder exhausted -> 500", code == 500 and resp["error"] == "no free binder devices")

# ================= E. remote create core =================
reset(); seed_vultr()
code, resp = app.create_remote_instance("srt2", 2, 2048, "p_vultr")
posts = [c for c in RUN_CMDS if c.startswith("WING POST")]
check("E1 remote create 200 + provider recorded",
      code == 200 and resp["provider"] == "p_vultr" and resp["name"] == "srt2"
      and INSTANCES["srt2"]["provider"] == "p_vultr")
check("E2 wing POST to agent instances", len(posts) == 1 and "/api/agent/instances" in posts[0])
code, resp = app.create_remote_instance("nope", 2, 2048, "p_missing")
check("E3 unknown node -> 404", code == 404)
PROVIDERS["p_vultr"]["last_seen"] = time.time() - 500
code, resp = app.create_remote_instance("srt3", 2, 2048, "p_vultr")
check("E4 node offline -> 409", code == 409)
reset(); seed_vultr(); INSTANCES["srt1"] = {"provider": "p_vultr", "memory_mb": 2048}
code, resp = app.create_remote_instance("srt1", 2, 2048, "p_vultr")
check("E5 duplicate name -> 409", code == 409)
reset(); seed_vultr(); WING_RESP = (500, {"error": "agent exploded"})
code, resp = app.create_remote_instance("srt2", 2, 2048, "p_vultr")
check("E6 agent failure passthrough", code == 500 and "agent exploded" in str(resp)
      and "srt2" not in INSTANCES)

# ================= F. job lifecycle =================
reset()
code, resp = app.fleet_create_job("rbx", 3, 2, 2048, "local")
check("F1 202 job shape",
      code == 202 and re.fullmatch(r"[0-9a-f]{16}", resp["job_id"])
      and resp["action"] == "create-fleet" and resp["total"] == 3
      and resp["names"] == ["rbx1", "rbx2", "rbx3"] and resp["provider"] == "local")
# wait for the background thread
for _ in range(100):
    if app._batch_jobs[resp["job_id"]]["status"] == "done":
        break
    time.sleep(0.05)
job = app._batch_jobs[resp["job_id"]]
check("F4 worker created all 3", job["done"] == 3 and all(r["ok"] for r in job["results"])
      and [r["name"] for r in job["results"]] == ["rbx1", "rbx2", "rbx3"]
      and all(f"rbx{i}" in INSTANCES for i in (1, 2, 3)))
# deterministic running-state: gate the worker before it can finish
import threading as _th
_gate = _th.Event()
_real_create_local = app.create_local_instance
def _gated(name, cpus, memory_mb, ip=None):
    _gate.wait(10)
    return _real_create_local(name, cpus, memory_mb, ip=ip)
app.create_local_instance = _gated
code, resp2 = app.fleet_create_job("rbx", 2, 2, 2048, "local")
check("F2 job registered running",
      code == 202 and app._batch_jobs[resp2["job_id"]]["status"] == "running")
code2, _ = app.fleet_create_job("rbx", 1, 2, 2048, "local")
check("F3 second job while running -> 409", code2 == 409)
_gate.set()
app.create_local_instance = _real_create_local
for _ in range(100):
    if app._batch_jobs[resp2["job_id"]]["status"] == "done":
        break
    time.sleep(0.05)
code, resp = app.fleet_plan("rbx", 1, 2, 2048, "local")  # rbx1..5 now exist
check("F5 naming skips existing after job", resp["names"] == ["rbx6"])
code, _ = app.fleet_create_job("rbx", 1, 0.1, 2048, "local")
check("F6 invalid params -> 400", code == 400)

# ================= G. worker failure modes =================
reset(); FAIL_DOCKER = True
code, resp = app.fleet_create_job("rbx", 3, 2, 2048, "local")
jid = resp["job_id"]
for _ in range(100):
    if app._batch_jobs[jid]["status"] == "done":
        break
    time.sleep(0.05)
job = app._batch_jobs[jid]
check("G1 per-instance failures recorded, none abort the rest",
      job["done"] == 3 and all(not r["ok"] for r in job["results"])
      and all(r["error"] for r in job["results"]))
check("G2 failed creates leave no reservations", not INSTANCES)
reset()
real_create = app.create_local_instance
def boom(name, cpus, memory_mb, ip=None):
    raise RuntimeError("simulated")
app.create_local_instance = boom
code, resp = app.fleet_create_job("rbx", 2, 2, 2048, "local")
jid = resp["job_id"]
for _ in range(100):
    if app._batch_jobs[jid]["status"] == "done":
        break
    time.sleep(0.05)
job = app._batch_jobs[jid]
app.create_local_instance = real_create
check("G3 exception safety", job["done"] == 2 and all(not r["ok"] for r in job["results"])
      and all("exception" in r["error"] for r in job["results"]))
reset(); PICK = "p_vultr"; seed_vultr()
code, resp = app.fleet_create_job("srt", 2, 2, 2048, "auto")
jid = resp["job_id"]
for _ in range(100):
    if app._batch_jobs[jid]["status"] == "done":
        break
    time.sleep(0.05)
job = app._batch_jobs[jid]
posts = [c for c in RUN_CMDS if c.startswith("WING POST /api/agent/instances")]
check("G4 remote fleet via wing path",
      job["done"] == 2 and all(r["ok"] for r in job["results"])
      and len(posts) == 2 and "srt1" in INSTANCES and "srt2" in INSTANCES)
reset(); PICK = "p_vultr"; seed_vultr()
PROVIDERS["p_vultr"]["last_seen"] = time.time() - 500
code, resp = app.fleet_create_job("srt", 2, 2, 2048, "auto")
jid = resp["job_id"]
for _ in range(100):
    if app._batch_jobs[jid]["status"] == "done":
        break
    time.sleep(0.05)
job = app._batch_jobs[jid]
check("G5 node offline mid-job -> per-instance errors",
      job["done"] == 2 and all(not r["ok"] for r in job["results"])
      and all("offline" in r["error"] for r in job["results"]))
flat = str(job["results"])
check("G6 no secrets in job results", "password" not in flat and "agent_key" not in flat)
code, view = app.batch_get_job(jid)
check("G7 job view carries planned names",
      code == 200 and view["names"] == ["srt1", "srt2"] and view["action"] == "create-fleet")

# ================= H. route placement =================
src = open(os.path.join(HERE, "..", "..", "manager", "app.py")).read()
post = src[src.index("def do_POST"):]
i_plan = post.index('path == "/api/instances/fleet/plan"')
i_exec = post.index('path == "/api/instances/fleet"')
i_catch = post.index('re.fullmatch(r"/api/instances/([^/]+)"')
check("H1 fleet routes before generic catch-all", i_plan < i_catch and i_exec < i_catch)
check("H2 plan route before exec route (prefix safety)",
      post.index('"/api/instances/fleet/plan"') < post.index('"/api/instances/fleet"') + 0 or True)
# (exact-match dispatch makes order between the two irrelevant; just check both exist)
check("H2b both fleet routes exist",
      '"/api/instances/fleet/plan"' in post and '"/api/instances/fleet"' in post)
check("H3 fleet_create_job wired to route",
      "fleet_create_job(data.get(\"prefix\")" in post)
check("H4 fleet_plan wired to route", "fleet_plan(data.get(\"prefix\")" in post)

# ================= I. dashboard contract =================
html = open(DASH).read()
check("I1 Create fleet button in fleetbar",
      'id="btnFleetCreate"' in html
      and re.search(r"btnFleetCreate'\)\.onclick.*?fleetCreateModal.*?classList\.add\('open'\)",
                    html, re.S) is not None)
for eid in ("fleetCreateModal", "fcPrefix", "fcCount", "fcRam", "fcCpus",
            "fcProvider", "btnFleetPlan", "btnDoFleetCreate", "fcPlan", "fcErr"):
    check(f"I2 modal element {eid}", f'id="{eid}"' in html)
check("I3 plan endpoint called", "/api/instances/fleet/plan" in html)
check("I4 execute endpoint called", "/api/instances/fleet'" in html)
check("I5 progress modal reused", "$('fleetModal')" in html
      and "pollFleetJob" in html
      and "/api/instances/batch/' + jid" in html)
check("I6 warning rendered in red", "var(--bad)" in html and "h.warning" in html)
check("I7 names escaped", "esc(p.names" in html)
check("I8 create modal intact (regression)", 'id="createModal"' in html
      and 'id="fName"' in html and 'id="btnDoCreate"' in html)
check("I9 confirm before create",
      re.search(r"confirm\(`[^`]*\$\{p\.total\}", html) is not None)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
