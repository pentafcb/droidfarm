"""Unit tests for live per-instance resource usage (manager/app.py +
dashboard fleet bar / card usage).

Run: python3 test_instance_resources.py
Covers: parse_size_mb units/garbage, parse_docker_stats (wd- filter, unsafe
names, malformed lines), cache fill/staleness/pruning, poll-once never raises,
poller never dies, instance_resources TTL behavior, fleet_resource_summary
aggregation, /api/instances field contract (local + remote branches),
dashboard contract (resCov span, updateResCov, card usage block, ubar CSS).

Hermetic: subprocess/docker never touched (fake docker_stats_fn injected);
time.sleep patched out of the poller test; /proc/meminfo read is monkeypatched
for aggregation tests.
"""
import importlib.util
import os
import re
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))

def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

app = load("resources_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
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

# ---------- parse_size_mb ----------
check("size MiB", app.parse_size_mb("412.5MiB") == 412.5)
check("size GiB", abs(app.parse_size_mb("1.2GiB") - 1228.8) < 0.01)
check("size KiB", abs(app.parse_size_mb("512KiB") - 0.5) < 1e-6)
check("size B", abs(app.parse_size_mb("1048576B") - 1.0) < 1e-6)
check("size MB plain", app.parse_size_mb("999MB") == 999.0)
check("size lowercase mib", app.parse_size_mb("100mib") == 100.0)
check("size GB no-i", abs(app.parse_size_mb("2GB") - 2048.0) < 1e-6)
check("size garbage -> None", app.parse_size_mb("n/a") is None)
check("size empty -> None", app.parse_size_mb("") is None)
check("size unit-only -> None", app.parse_size_mb("MiB") is None)
check("size bad unit -> None", app.parse_size_mb("10XB") is None)
check("size -- placeholder -> None", app.parse_size_mb("--") is None)

STATS = (
    "wd-rbx1\t412.5MiB / 2GiB\t12.34%\n"
    "wd-rbx2\t1.1GiB / 2GiB\t45%\n"
    "unrelated\t10MiB / 1GiB\t1%\n"          # not wd- -> skipped
    "wd-bad;name\t10MiB / 1GiB\t1%\n"        # unsafe name -> skipped
    "wd-rbx3\tgarbage\t2%\n"                 # bad mem -> skipped
    "wd-rbx4\t10MiB / 1GiB\tnope\n"          # bad cpu -> skipped
    "malformed line no tabs\n"
    "wd-rbx5\t10MiB\t3%\n"                   # mem without limit -> skipped
)

# ---------- parse_docker_stats ----------
p = app.parse_docker_stats(STATS)
check("stats rbx1 parsed", p.get("rbx1") == {"mem_used_mb": 412.5, "cpu_pct": 12.3})
check("stats rbx2 parsed", abs(p["rbx2"]["mem_used_mb"] - 1126.4) < 0.05 and p["rbx2"]["cpu_pct"] == 45.0)
check("stats unrelated skipped", "unrelated" not in p)
check("stats unsafe name skipped", "bad;name" not in p)
check("stats garbage mem skipped", "rbx3" not in p)
check("stats bad cpu skipped", "rbx4" not in p)
check("stats mem-no-limit skipped", "rbx5" not in p)
check("stats empty -> {}", app.parse_docker_stats("") == {})
check("stats None -> {}", app.parse_docker_stats(None) == {})

# ---------- cache fill / staleness / pruning ----------
with app._resource_lock:
    app._resource_cache.clear()
app._resource_poll_once(docker_stats_fn=lambda: "wd-rbx1\t100MiB / 2GiB\t5%\n")
r = app.instance_resources("rbx1")
check("cache fill", r == {"mem_used_mb": 100.0, "cpu_pct": 5.0})
check("cache unknown name -> None", app.instance_resources("nope") is None)

# force staleness
with app._resource_lock:
    app._resource_cache["rbx1"]["ts"] -= (app._RESOURCE_TTL + 1)
check("stale entry -> None", app.instance_resources("rbx1") is None)
app._resource_poll_once(docker_stats_fn=lambda: "wd-rbx2\t50MiB / 2GiB\t1%\n")
with app._resource_lock:
    still = "rbx1" in app._resource_cache
check("stale entry pruned on next poll", not still)
check("fresh entry survives", app.instance_resources("rbx2") == {"mem_used_mb": 50.0, "cpu_pct": 1.0})

# poll never raises on failing source
def boom():
    raise RuntimeError("docker down")
before = dict(app._resource_cache)
app._resource_poll_once(docker_stats_fn=boom)  # must not raise
check("failing source keeps cache", app._resource_cache == before)

# poller loop never dies on poll exceptions
calls = []
sleeps = []
orig_poll = app._resource_poll_once
orig_sleep = app.time.sleep
class StopLoop(Exception):
    pass
def fake_poll():
    calls.append(1)
    raise RuntimeError("boom")  # every poll fails: loop must survive
def fake_sleep(s):
    sleeps.append(1)
    if len(sleeps) >= 3:
        raise StopLoop()
app._resource_poll_once = fake_poll
app.time.sleep = fake_sleep
try:
    app.resource_poller()
except StopLoop:
    pass
finally:
    app._resource_poll_once = orig_poll
    app.time.sleep = orig_sleep
check("poller survives poll exceptions (3 polls)", len(calls) == 3)

# ---------- instance_resources never raises ----------
with app._resource_lock:
    app._resource_cache.clear()
check("instance_resources empty cache -> None", app.instance_resources("rbx1") is None)

# ---------- fleet_resource_summary ----------
with app._resource_lock:
    now = app.time.time()
    app._resource_cache.clear()
    app._resource_cache["a"] = {"mem_used_mb": 400.0, "cpu_pct": 10.0, "ts": now}
    app._resource_cache["b"] = {"mem_used_mb": 800.5, "cpu_pct": 20.25, "ts": now}
    app._resource_cache["stale"] = {"mem_used_mb": 9999.0, "cpu_pct": 99.0,
                                    "ts": now - app._RESOURCE_TTL - 5}
orig_host = app.host_mem_total_mb
app.host_mem_total_mb = lambda: 31744
try:
    s = app.fleet_resource_summary()
finally:
    app.host_mem_total_mb = orig_host
    with app._resource_lock:
        app._resource_cache.clear()
check("fleet sums mem (stale excluded)", s["mem_used_mb"] == 1200.5)
check("fleet sums cpu", abs(s["cpu_pct"] - 30.2) < 0.05)
check("fleet instances_seen counts fresh only", s["instances_seen"] == 2)
check("fleet carries host_mem_mb", s["host_mem_mb"] == 31744)
check("fleet shape keys", set(s) == {"mem_used_mb", "cpu_pct", "instances_seen", "host_mem_mb"})

# summary never raises even if host read fails
app.host_mem_total_mb = lambda: (_ for _ in ()).throw(OSError("x"))
try:
    s2 = app.fleet_resource_summary()
    check("fleet summary survives host-read failure", s2["host_mem_mb"] is None)
finally:
    app.host_mem_total_mb = orig_host

# ---------- app.py API contract (source-level, like other suites) ----------
SRC = open(os.path.join(HERE, "..", "..", "manager", "app.py")).read()
check("local branch emits mem_used_mb", '"mem_used_mb": res["mem_used_mb"] if res else None' in SRC)
check("local branch emits cpu_pct", '"cpu_pct": res["cpu_pct"] if res else None' in SRC)
check("remote branch emits agent usage (sanitized)",
      '"mem_used_mb": clean_agent_resource(rs.get("mem_used_mb"))' in SRC
      and '"cpu_pct": clean_agent_resource(rs.get("cpu_pct"))' in SRC)
check("response carries fleet_resources",
      '"fleet_resources": fleet_resource_summary(' in SRC
      and "remote_instances=remote_entries" in SRC
      and "remote_host_ram_mb=remote_host_ram_mb(providers)" in SRC)
check("poller thread started in main", 'target=resource_poller' in SRC)
check("poller daemon thread", 'name="resource-poller"' in SRC)

# ---------- dashboard contract ----------
check("dash resCov span", 'id="resCov"' in DASH_SRC)
check("dash updateResCov fn", 'function updateResCov(fr)' in DASH_SRC)
check("dash refresh reads fleet_resources", 'updateResCov(j.fleet_resources)' in DASH_SRC)
_rescov = re.search(r"function updateResCov\(fr\) \{(.*?)\n\}", DASH_SRC, re.S)
check("dash fleet RAM label",
      _rescov is not None and all(k in _rescov.group(1) for k in
                                 ("fr.mem_used_mb", "fr.host_mem_mb",
                                  "fr.cpu_pct", "fr.instances_seen", "' GB")))
check("dash card usage block", 'usageHtml' in DASH_SRC)
check("dash ubar css", '.ubar' in DASH_SRC)
check("dash warm/hot thresholds", "'warm'" in DASH_SRC and "'hot'" in DASH_SRC)
check("dash pct>=90 hot", 'pct >= 90' in DASH_SRC)
check("dash null-safe usage", 'i.mem_used_mb != null' in DASH_SRC)
check("dash cpu rounding", 'Math.round(i.cpu_pct)' in DASH_SRC)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
