"""Unit tests for fleet CPU headroom (Create-fleet plan preview).

Gap: fleet_headroom() warned on memory overcommit and binder/loop/image
shortfalls, but never on CPU. The fleet-create plan takes a per-instance
`cpus` limit and never summed it against host CPUs — on the 8-vCPU box,
creating 5 more 2-CPU instances silently pushed CPU limits past 2x the
core count. Docker --cpus is a quota (shares), not a reservation, so this
never hard-fails: it just makes every instance slower under load. The plan
preview now warns (never refuses) before anything is created.

Covers: local branch math/warning, remote branch (host_cpus from the
agent's heartbeat stats, which carry cpu_count since the agent's
get_host_stats), fractional cpus, cpus=None legacy-call regression (no CPU
warning, existing 4-arg suites keep passing), host_cpus=0 fail-open,
garbage-cpus never-raises, instances.json without "cpus" keys, other
providers untouched, fleet_plan cpus passthrough, and the dashboard
contract (no dashboard change needed — the CPU warning rides the existing
headroom.warning string rendered by the plan modal).

Run: python3 test_fleet_cpu_headroom.py

Hermetic: os.cpu_count, app.get_host_stats, app.load_instances,
app.load_providers, app.run, app.count_loop_devices,
app.waydroid_image_present, app.probe_remote_headroom are monkeypatched;
no subprocesses, no network, no filesystem beyond the repo.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
DASH = os.path.join(REPO, "manager", "static", "index.html")

spec = importlib.util.spec_from_file_location("app_under_test", APP)
app = importlib.util.module_from_spec(spec)
sys.modules["app_under_test"] = app
spec.loader.exec_module(app)

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name)


# ---------------- fixtures ----------------
INSTANCES = {}
PROVIDERS = {}
HOST_RAM_MB = 31744
HOST_CPUS = 8
RUN_CMDS = []


def reset():
    global HOST_RAM_MB, HOST_CPUS
    INSTANCES.clear()
    PROVIDERS.clear()
    HOST_RAM_MB = 31744
    HOST_CPUS = 8
    RUN_CMDS.clear()


def seed_local(n, cpus=2):
    for i in range(1, n + 1):
        INSTANCES[f"rbx{i}"] = {"provider": "local", "memory_mb": 2048,
                                "cpus": cpus}


def fake_load_instances():
    return INSTANCES


def fake_load_providers():
    return PROVIDERS


def fake_get_host_stats():
    return {"ram_total_mb": HOST_RAM_MB, "ram_used_mb": 0,
            "ram_percent": 0.0, "cpu_percent": 0.0}


def fake_run(cmd, timeout=10):
    RUN_CMDS.append(cmd)
    if cmd.startswith("ls /dev/binderfs/"):
        return 0, "binder binder1 binder2", ""
    if cmd.startswith("ls -d /dev/loop"):
        return 0, "/dev/loop0 /dev/loop1", ""
    if cmd.startswith("docker image inspect"):
        return 0, "{}", ""
    return 1, "", "nope"


def fake_probe_remote_headroom(hr, provider_id):
    hr["image_present"] = True
    hr["loop_devices"] = 64


app.load_instances = fake_load_instances
app.load_providers = fake_load_providers
app.get_host_stats = fake_get_host_stats
app.run = fake_run
app.count_loop_devices = lambda: 64
app.waydroid_image_present = lambda: True
app.probe_remote_headroom = fake_probe_remote_headroom
_orig_cpu_count = os.cpu_count
os.cpu_count = lambda: HOST_CPUS

# ================= A. local branch =================
reset(); seed_local(2, 2)  # 4 CPU limits + 2x2 = 8, exactly host
hr = app.fleet_headroom(fake_load_instances(), "local", 2, 2048, cpus=2)
check("A1 fits: fields + no CPU warning",
      hr["host_cpus"] == 8 and hr["current_cpus"] == 4
      and hr["new_cpus"] == 8 and hr["cpu_overcommit"] == 0
      and "CPU limits" not in hr["warning"])

reset(); seed_local(3, 2)  # 6 current + 3x2 = 12 > 8
hr = app.fleet_headroom(fake_load_instances(), "local", 3, 2048, cpus=2)
check("A2 overcommit: numbers + warning text",
      hr["cpu_overcommit"] == 4 and hr["new_cpus"] == 12
      and "CPU limits would exceed host CPUs by 4" in hr["warning"]
      and "12 CPU limits on 8 CPU host" in hr["warning"]
      and "shares, not reservations" in hr["warning"])

reset(); seed_local(4, 2)  # 8 current, adding 0.5x2 = +1
hr = app.fleet_headroom(fake_load_instances(), "local", 2, 2048, cpus=0.5)
check("A3 fractional cpus",
      hr["new_cpus"] == 9 and hr["cpu_overcommit"] == 1
      and "exceed host CPUs by 1" in hr["warning"])

reset(); seed_local(10, 2)  # 20 limits on 8 -> over by 12, legacy call
hr = app.fleet_headroom(fake_load_instances(), "local", 5, 2048)
check("A4 cpus=None: no CPU warning (4-arg contract)",
      "CPU limits" not in hr["warning"] and hr["cpu_overcommit"] == 0
      and hr["host_cpus"] == 8 and hr["current_cpus"] == 20
      and hr["new_cpus"] == 20)

reset(); seed_local(10, 2); HOST_CPUS = 0  # os.cpu_count unknown
hr = app.fleet_headroom(fake_load_instances(), "local", 5, 2048, cpus=2)
check("A5 host_cpus=0: fail-open, no warning",
      hr["host_cpus"] == 0 and hr["cpu_overcommit"] == 0
      and "CPU limits" not in hr["warning"])
reset()

reset(); seed_local(3, 2)
hr = app.fleet_headroom(fake_load_instances(), "local", 2, 2048, cpus="abc")
check("A6 garbage cpus: never raises, fail-open",
      isinstance(hr, dict) and hr["warning"] == ""
      and hr["cpu_overcommit"] == 0)
reset()

# instances without "cpus" keys (older instances.json) -> treated as 0
INSTANCES["rbx1"] = {"provider": "local", "memory_mb": 2048}
hr = app.fleet_headroom(fake_load_instances(), "local", 2, 2048, cpus=2)
check("A7 missing cpus key tolerated",
      hr["current_cpus"] == 0 and hr["new_cpus"] == 4)
reset()

# string cpus tolerated
INSTANCES["rbx1"] = {"provider": "local", "memory_mb": 2048, "cpus": "2"}
hr = app.fleet_headroom(fake_load_instances(), "local", 1, 2048, cpus=2)
check("A8 string cpus tolerated", hr["current_cpus"] == 2)
reset()

# other providers' instances not counted locally
INSTANCES["rbx1"] = {"provider": "local", "memory_mb": 2048, "cpus": 2}
INSTANCES["srt1"] = {"provider": "p_vultr", "memory_mb": 2048, "cpus": 16}
hr = app.fleet_headroom(fake_load_instances(), "local", 1, 2048, cpus=2)
check("A9 remote instances excluded from local sum",
      hr["current_cpus"] == 2 and hr["new_cpus"] == 4)
reset()

# memory overcommit warning still fires alongside CPU
reset(); HOST_RAM_MB = 2048; seed_local(10, 2)
hr = app.fleet_headroom(fake_load_instances(), "local", 5, 2048, cpus=2)
check("A10 memory + CPU warnings coexist",
      "memory limits would exceed" in hr["warning"]
      and "CPU limits would exceed" in hr["warning"])
reset()

# ================= B. remote branch =================
PROVIDERS["p_vultr"] = {"name": "vultr", "last_seen": 1e18,
                        "stats": {"ram_total_mb": 8000, "cpu_count": 4,
                                  "ram_percent": 10.0}}
INSTANCES["srt1"] = {"provider": "p_vultr", "memory_mb": 2048, "cpus": 2}
INSTANCES["rbx1"] = {"provider": "local", "memory_mb": 2048, "cpus": 2}
hr = app.fleet_headroom(fake_load_instances(), "p_vultr", 2, 2048, cpus=2)
check("B1 remote: host_cpus from heartbeat stats, local excluded",
      hr["host_cpus"] == 4 and hr["current_cpus"] == 2
      and hr["new_cpus"] == 6 and hr["cpu_overcommit"] == 2
      and "exceed host CPUs by 2" in hr["warning"]
      and "6 CPU limits on 4 CPU host" in hr["warning"])
reset()

PROVIDERS["p_old"] = {"name": "old", "last_seen": 1e18,
                      "stats": {"ram_total_mb": 8000}}  # no cpu_count
INSTANCES["srt1"] = {"provider": "p_old", "memory_mb": 2048, "cpus": 16}
hr = app.fleet_headroom(fake_load_instances(), "p_old", 2, 2048, cpus=2)
check("B2 remote: no cpu_count -> fail-open, no CPU warning",
      hr["host_cpus"] == 0 and hr["cpu_overcommit"] == 0
      and "CPU limits" not in hr["warning"])
reset()

PROVIDERS["p_nostats"] = {"name": "nostats", "last_seen": 1e18}
hr = app.fleet_headroom(fake_load_instances(), "p_nostats", 2, 2048, cpus=2)
check("B3 remote: no stats at all -> fail-open",
      hr["host_cpus"] == 0 and "CPU limits" not in hr["warning"])
reset()

# cpus=None remote legacy call: no warning
PROVIDERS["p_vultr"] = {"name": "vultr", "last_seen": 1e18,
                        "stats": {"ram_total_mb": 8000, "cpu_count": 4}}
INSTANCES["srt1"] = {"provider": "p_vultr", "memory_mb": 2048, "cpus": 16}
hr = app.fleet_headroom(fake_load_instances(), "p_vultr", 2, 2048)
check("B4 remote cpus=None: no CPU warning",
      "CPU limits" not in hr["warning"] and hr["cpu_overcommit"] == 0)
reset()

# ================= C. fleet_plan passthrough =================
reset(); HOST_CPUS = 4; seed_local(3, 2)  # 6 current + 2x2 = 10 > 4
code, resp = app.fleet_plan("rbx", 2, 2, 2048, "local")
check("C1 fleet_plan carries cpus into headroom",
      code == 200 and resp["cpus"] == 2
      and "CPU limits would exceed host CPUs by 6" in resp["headroom"]["warning"]
      and resp["headroom"]["new_cpus"] == 10)
reset()

# ================= D. contracts =================
src = open(APP).read()
i = src.index("def fleet_headroom(")
body = src[i:i + 2500]
check("D1 no docker start/stop/rm/volume in CPU code",
      "docker start" not in body and "docker rm" not in body
      and "volume" not in body)
check("D2 never-raises contract kept",
      "Best-effort: never raises" in src[src.index("def fleet_headroom("):][:400]
      or "never raises" in body)
check("D3 dashboard: plan modal renders headroom.warning (CPU rides it)",
      "p.headroom.warning" in open(DASH).read())

# secret hygiene
flat = str(app.fleet_headroom(fake_load_instances(), "local", 1, 2048, cpus=2))
check("D4 no secrets in headroom dict",
      "password" not in flat and "token" not in flat and "agent_key" not in flat)

os.cpu_count = _orig_cpu_count
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
