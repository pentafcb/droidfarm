"""Unit tests for the create-preflight image check.

Gap: on a host without the waydroid-instance image built (the new VPS
64.177.49.68), creating an instance failed deep inside `docker run` with a
cryptic docker error. Now create_local_instance() refuses fast with an
actionable message (reservation rolled back), and the fleet-create plan
endpoint warns about the missing image BEFORE anything is created.

Covers: waydroid_image_present() (present/missing/daemon-down/unknown),
create_local_instance fail-fast + rollback + fail-open, binder-missing
regression, fleet_headroom image warning (local/remote/unknown), and the
dashboard/route contracts.

Run: python3 test_create_preflight.py

Hermetic: load_instances/save_instances/run/next_free_ip are monkeypatched;
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

app = load("createpreflight_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
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
BINDER_DEVS = ["binder"] + [f"binder{i}" for i in range(1, 40)]  # 40 devices
BINDER_LS = " ".join(BINDER_DEVS)
IMAGE_MODE = "present"  # present | missing | daemon-down | empty-err | raise
WING_HEADROOM = (200, {"ok": True, "image_present": True, "loop_devices": 64})

def fake_load_instances():
    return {k: dict(v) for k, v in INSTANCES.items()}
def fake_save_instances(d):
    INSTANCES.clear()
    INSTANCES.update({k: dict(v) for k, v in d.items()})
def fake_load_providers():
    return {k: dict(v) for k, v in PROVIDERS.items()}
def fake_run(cmd, timeout=30):
    RUN_CMDS.append(cmd)
    if cmd.startswith("docker image inspect"):
        if IMAGE_MODE == "present":
            return 0, '[{"Id":"sha256:abc"}]', ""
        if IMAGE_MODE == "missing":
            return 1, "", "Error: No such image: waydroid-instance:latest"
        if IMAGE_MODE == "daemon-down":
            return 1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
        if IMAGE_MODE == "empty-err":
            return 1, "", ""
        raise RuntimeError("docker exploded")
    if cmd.startswith("ls /dev/binderfs/"):
        return 0, BINDER_LS, ""
    if cmd.startswith("docker inspect"):
        return 1, "", ""          # container does not exist
    if cmd.startswith("docker run"):
        return 0, "cid123", ""
    return 0, "", ""
def fake_next_free_ip(instances):
    used = {v.get("ip") for v in instances.values() if v.get("ip")}
    for i in range(11, 250):
        ip = f"10.20.0.{i}"
        if ip not in used:
            return ip
    return None
def fake_get_host_stats():
    return {"ram_total_mb": 31744, "ram_percent": 50.0}
def fake_wing_request(p, method, path, data=None, timeout=8, attempts=3):
    # headroom probe (remote-headroom): the plan preview must stay hermetic
    if method == "GET" and path == "/api/agent/headroom":
        return WING_HEADROOM
    return (200, {"ok": True})

def reset(mode="present", binder_ls=BINDER_LS):
    global RUN_CMDS, IMAGE_MODE, BINDER_LS, WING_HEADROOM
    RUN_CMDS = []
    IMAGE_MODE = mode
    BINDER_LS = binder_ls
    WING_HEADROOM = (200, {"ok": True, "image_present": True, "loop_devices": 64})
    INSTANCES.clear()
    PROVIDERS.clear()
    app.load_instances = fake_load_instances
    app.save_instances = fake_save_instances
    app.load_providers = fake_load_providers
    app.run = fake_run
    app.next_free_ip = fake_next_free_ip
    app.get_host_stats = fake_get_host_stats
    app.wing_request = fake_wing_request

def seed_vultr():
    PROVIDERS["p_vultr"] = {"url": "http://10.0.0.2:8090", "agent_key": "k",
                           "last_seen": time.time(),
                           "stats": {"ram_total_mb": 8000, "ram_percent": 30.0}}

def _gate_before_run(src):
    # within create_local_instance, the image gate must precede `docker run`
    m = re.search(r"def create_local_instance\(.*?\n(?=def |\Z)", src, re.S)
    if not m:
        return False
    body = m.group(0)
    gi = body.find("waydroid_image_present() is False")
    # Match the actual command ("docker run -d"), not the prose phrase
    # "docker run": the create-sink-gate comment inside this same body
    # describes the shell command in words, and the bare substring hit
    # the comment before the image gate (test-only fix, no product
    # change — the gate still precedes the real command).
    ri = body.find("docker run -d")
    return gi != -1 and ri != -1 and gi < ri

# ================= A. waydroid_image_present() =================
reset("present")
check("A1 image present -> True", app.waydroid_image_present() is True)

reset("missing")
check("A2 no-such-image -> False", app.waydroid_image_present() is False)

reset("daemon-down")
check("A3 daemon down -> None (fail-open)", app.waydroid_image_present() is None)

reset("empty-err")
check("A4 rc=1 empty stderr -> None (fail-open)",
      app.waydroid_image_present() is None)

reset("raise")
check("A5 run() raising -> None, never raises",
      app.waydroid_image_present() is None)

# ================= B. create_local_instance =================
reset("missing")
code, resp = app.create_local_instance("srt1", 2, 2048)
check("B1 missing image -> 500", code == 500)
check("B2 error names the image + the fix",
      "image" in resp.get("error", "") and "build" in resp.get("error", ""))
check("B3 reservation rolled back (instances.json clean)",
      INSTANCES == {})
check("B4 docker run never attempted on missing image",
      not any(c.startswith("docker run") for c in RUN_CMDS))

reset("present")
code, resp = app.create_local_instance("srt1", 2, 2048)
check("B5 image present -> 200, docker run attempted",
      code == 200 and any(c.startswith("docker run") for c in RUN_CMDS)
      and "srt1" in INSTANCES)

reset("daemon-down")
code, resp = app.create_local_instance("srt2", 2, 2048)
check("B6 unknown image state -> fail-open, create proceeds",
      code == 200 and "srt2" in INSTANCES)

reset("present", binder_ls="")  # no binder devices at all
code, resp = app.create_local_instance("srt3", 2, 2048)
check("B7 binder-missing regression: 500 'no free binder devices'",
      code == 500 and resp.get("error") == "no free binder devices")
check("B8 image error constant is actionable",
      isinstance(app.IMAGE_MISSING_ERROR, str)
      and "waydroid-instance" in app.IMAGE_MISSING_ERROR
      and "build" in app.IMAGE_MISSING_ERROR)

# ================= C. fleet_headroom =================
reset("missing")
hr = app.fleet_headroom({}, "local", 5, 2048)
check("C1 local + missing image -> image_present False",
      hr.get("image_present") is False)
check("C2 local + missing image -> warning names the image",
      "image" in hr.get("warning", ""))

reset("present")
hr = app.fleet_headroom({}, "local", 5, 2048)
check("C3 local + present -> True, no image warning",
      hr.get("image_present") is True and "image" not in hr.get("warning", ""))

reset("daemon-down")
hr = app.fleet_headroom({}, "local", 5, 2048)
check("C4 local + unknown -> None, no image warning",
      hr.get("image_present") is None and "image" not in hr.get("warning", ""))

reset("present")
seed_vultr()
hr = app.fleet_headroom({}, "p_vultr", 5, 2048)
check("C5 remote node -> headroom probe fills image/loop, no local docker probe",
      hr.get("image_present") is True and hr.get("loop_devices") == 64
      and hr.get("headroom_probe") == "ok"
      and "image" not in hr.get("warning", "")
      and not any(c.startswith("docker image inspect") for c in RUN_CMDS))

reset("raise")
try:
    hr = app.fleet_headroom({}, "local", 5, 2048)
    raised = False
except Exception:
    raised = True
check("C6 headroom never raises when docker explodes", not raised)

# ================= D. contracts =================
html = open(DASH, encoding="utf-8").read()
check("D1 plan modal renders headroom.warning",
      "p.headroom.warning" in html)
src = open(os.path.join(HERE, "..", "..", "manager", "app.py"),
           encoding="utf-8").read()
check("D2 /api/instances/fleet/plan route exists",
      '"/api/instances/fleet/plan"' in src)
check("D3 create core calls the image gate before docker run",
      _gate_before_run(src))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
