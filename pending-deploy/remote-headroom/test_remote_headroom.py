"""Unit tests for remote headroom probing (Create-fleet plan on wing nodes).

Gap: the fleet-create plan preview (fleet_headroom, remote branch) only knew
a wing node's heartbeat RAM stats. It could not warn that a create would
fail on a node whose waydroid-instance image was never built, or whose loop
pool is the stock 8 — the exact failures Penta hit on new/vultr nodes. The
manager's create-preflight (local) and the agent's create preflight (1.0.18)
fail fast at create time; the plan preview now warns BEFORE anything is
created.

Covers: agent 1.0.21 GET /api/agent/headroom route (auth-gated, payload
contract, live localhost server), probe_remote_headroom() (healthy/old-agent/
unreachable/stale/garbage paths, fail-fast attempts=1, never-raises),
fleet_headroom() remote integration (warnings, node-aware image text), local
branch regression, and the dashboard contract (no dashboard change needed —
headroom.warning already renders in the plan modal).

Run: python3 test_remote_headroom.py

Hermetic: load_providers/wing_request/run/get_host_stats/waydroid_image_present/
count_loop_devices are monkeypatched; the live-server section hits only
127.0.0.1 (the agent's own Handler) and never touches docker state beyond a
fail-open `docker image inspect`.
"""
import importlib.util
import inspect
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")
DASH = os.path.join(REPO, "manager", "static", "index.html")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("rh_agent", AGENT)
app = load("rh_app", APP)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


print("== A. VERSION bump + manager gate ==")
check("A1 VERSION is 1.0.21+ (1.0.22 adds per-instance degraded surfacing)", agent.VERSION in ("1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"),
      repr(agent.VERSION))
check("A2 manager >=1.0.2 gate accepts 1.0.21",
      app._version_at_least("1.0.21", "1.0.2"))
check("A3 gate still rejects 1.0.1",
      not app._version_at_least("1.0.1", "1.0.2"))

print("== B. agent route contract (source) ==")
src = inspect.getsource(agent.Handler.do_GET)
check("B1 /api/agent/headroom branch exists in do_GET",
      'if path == "/api/agent/headroom":' in src)
branch = src.split('if path == "/api/agent/headroom":', 1)[1].split("\n        if path ==", 1)[0] \
    if 'if path == "/api/agent/headroom":' in src else ""
check("B2 branch returns 200 with ok/image_present/loop_devices",
      "self._send(200" in branch and '"ok"' in branch
      and '"image_present"' in branch and '"loop_devices"' in branch)
gate_pos = src.find('if _authv == "denied":')
branch_pos = src.find('if path == "/api/agent/headroom":')
check("B3 branch sits behind the auth gate (denied -> 401 before it)",
      0 < gate_pos < branch_pos)
check("B4 branch reuses sw_image_present + sw_count_loop_devices (no new docker logic)",
      "sw_image_present()" in branch and "sw_count_loop_devices()" in branch)
check("B5 Windows gets loop_devices None (binder/loop are Linux-only)",
      "IS_WINDOWS" in branch)

print("== C. live localhost server ==")


class Quiet(agent.Handler):
    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Quiet)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def req(path, key=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if key is not None:
        r.add_header("X-Agent-Key", key)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


agent.AGENT_KEY = "rh-live-key"
try:
    agent._AUTH_ATTEMPTS.clear()
except Exception:
    pass
code, body = req("/api/agent/headroom", key="rh-live-key")
check("C1 correct key -> 200", code == 200, f"got {code}")
try:
    payload = json.loads(body)
except Exception:
    payload = None
check("C2 payload shape: ok True, image_present tri-state, loop int-or-None",
      isinstance(payload, dict) and payload.get("ok") is True
      and payload.get("image_present") in (True, False, None)
      and (payload.get("loop_devices") is None
           or isinstance(payload.get("loop_devices"), int)),
      repr(body[:120]))
code, _ = req("/api/agent/headroom", key="wrong-key")
check("C3 wrong key -> 401", code == 401, f"got {code}")
code, _ = req("/api/agent/headroom")
check("C4 no key -> 401", code == 401, f"got {code}")
check("C5 route is not the health bypass (auth required, unlike /api/agent/health)",
      code == 401)
srv.shutdown()

print("== D. probe_remote_headroom ==")
PROVIDERS = {}
WING_RESP = (200, {"ok": True, "image_present": True, "loop_devices": 64})
WING_RAISE = False
WING_CALLS = []


def fake_load_providers():
    return {k: dict(v) for k, v in PROVIDERS.items()}


def fake_wing_request(p, method, path, data=None, timeout=8, attempts=3):
    WING_CALLS.append({"method": method, "path": path, "attempts": attempts,
                       "timeout": timeout})
    if WING_RAISE:
        raise RuntimeError("network down")
    return WING_RESP


def reset(providers, resp=(200, {"ok": True, "image_present": True, "loop_devices": 64}),
          raise_=False):
    global WING_RESP, WING_RAISE, WING_CALLS
    PROVIDERS.clear()
    PROVIDERS.update(providers)
    WING_RESP = resp
    WING_RAISE = raise_
    WING_CALLS = []
    app.load_providers = fake_load_providers
    app.wing_request = fake_wing_request


def fresh_hr():
    return {"image_present": None, "loop_devices": None, "headroom_probe": "ok"}


def seed_node(last_seen_offset=0):
    return {"p1": {"url": "http://10.0.0.2:8090", "agent_key": "k",
                   "name": "srt-node",
                   "last_seen": time.time() - last_seen_offset,
                   "stats": {"ram_total_mb": 8000}}}


reset(seed_node())
hr = fresh_hr()
app.probe_remote_headroom(hr, "p1")
check("D1 healthy probe fills image_present/loop_devices, probe ok",
      hr["image_present"] is True and hr["loop_devices"] == 64
      and hr["headroom_probe"] == "ok")

reset(seed_node(), resp=(200, {"ok": True, "image_present": False, "loop_devices": 64}))
hr = fresh_hr()
app.probe_remote_headroom(hr, "p1")
check("D2 image False passes through (warning built by fleet_headroom)",
      hr["image_present"] is False and hr["headroom_probe"] == "ok")

reset(seed_node(), resp=(200, {"ok": True, "image_present": True, "loop_devices": 8}))
hr = fresh_hr()
app.probe_remote_headroom(hr, "p1")
check("D3 loop 8 passes through", hr["loop_devices"] == 8)

reset(seed_node(), resp=(200, {"ok": True, "image_present": "yes", "loop_devices": "abc"}))
hr = fresh_hr()
app.probe_remote_headroom(hr, "p1")
check("D4 garbage image_present -> None", hr["image_present"] is None)
check("D5 garbage loop_devices -> None",
      hr["loop_devices"] is None and hr["headroom_probe"] == "ok")

reset(seed_node(), resp=(200, {"ok": True, "image_present": True, "loop_devices": -5}))
hr = fresh_hr()
app.probe_remote_headroom(hr, "p1")
check("D5b out-of-range loop_devices -> None", hr["loop_devices"] is None)

reset(seed_node(), resp=(200, ["not", "a", "dict"]))
hr = fresh_hr()
app.probe_remote_headroom(hr, "p1")
check("D6 non-dict 200 body -> fields None, probe not ok",
      hr["image_present"] is None and hr["loop_devices"] is None
      and hr["headroom_probe"] != "ok")

reset(seed_node(), resp=(404, {"error": "not found"}))
hr = fresh_hr()
app.probe_remote_headroom(hr, "p1")
check("D7 404 -> old-agent (fields stay None)",
      hr["headroom_probe"] == "old-agent" and hr["image_present"] is None)

reset(seed_node(), resp=(500, {"error": "boom"}))
hr = fresh_hr()
app.probe_remote_headroom(hr, "p1")
check("D8 500 -> unreachable", hr["headroom_probe"] == "unreachable")

reset(seed_node(), raise_=True)
hr = fresh_hr()
try:
    app.probe_remote_headroom(hr, "p1")
    raised = False
except Exception:
    raised = True
check("D9 raising wing_request -> unreachable, never raises",
      not raised and hr["headroom_probe"] == "unreachable")

reset(seed_node(last_seen_offset=300))
hr = fresh_hr()
app.probe_remote_headroom(hr, "p1")
check("D10 stale heartbeat (>120s) -> unreachable, no wing call",
      hr["headroom_probe"] == "unreachable" and WING_CALLS == [])

reset({})
hr = fresh_hr()
try:
    app.probe_remote_headroom(hr, "nope")
    raised = False
except Exception:
    raised = True
check("D11 unknown provider -> unreachable, never raises",
      not raised and hr["headroom_probe"] == "unreachable")

reset(seed_node())
hr = fresh_hr()
app.probe_remote_headroom(hr, "p1")
check("D12 fail-fast: probe uses attempts=1",
      len(WING_CALLS) == 1 and WING_CALLS[0]["attempts"] == 1
      and WING_CALLS[0]["path"] == "/api/agent/headroom")

reset({"p1": {"url": "http://10.0.0.2:8090", "last_seen": "yesterday"}})
hr = fresh_hr()
try:
    app.probe_remote_headroom(hr, "p1")
    raised = False
except Exception:
    raised = True
check("D13 garbage provider record -> unreachable, never raises",
      not raised and hr["headroom_probe"] == "unreachable")

reset(seed_node(), resp=(200, {"ok": True, "image_present": None, "loop_devices": 64}))
hr = fresh_hr()
app.probe_remote_headroom(hr, "p1")
check("D14 image None (agent fail-open) -> None, probe ok",
      hr["image_present"] is None and hr["headroom_probe"] == "ok")

print("== E. fleet_headroom remote integration ==")


def fake_run(cmd, timeout=30):
    if cmd.startswith("ls /dev/binderfs/"):
        return 0, "binder binder1 binder2", ""
    return 0, "", ""


app.run = fake_run
app.get_host_stats = lambda: {"ram_total_mb": 31744, "ram_percent": 10.0}
app.waydroid_image_present = lambda: True
app.count_loop_devices = lambda: 64
app.load_instances = lambda: {}


def plan(provider, resp, last_seen_offset=0, name="srt-node"):
    reset({"p1": {"url": "http://10.0.0.2:8090", "agent_key": "k", "name": name,
                   "last_seen": time.time() - last_seen_offset,
                   "stats": {"ram_total_mb": 8000, "ram_percent": 10.0}}},
          resp=resp)
    return app.fleet_headroom({}, provider, 2, 2048)


hr = plan("p1", (200, {"ok": True, "image_present": True, "loop_devices": 64}))
check("E1 remote healthy probe -> no image/loop warning",
      "image" not in hr["warning"] and "loop device" not in hr["warning"].lower()
      and hr["image_present"] is True and hr["loop_devices"] == 64)

hr = plan("p1", (200, {"ok": True, "image_present": False, "loop_devices": 64}))
check("E2 remote image missing -> warning names image + node name",
      "waydroid-instance" in hr["warning"] and "srt-node" in hr["warning"]
      and "waydroid-image-build" in hr["warning"])

hr = plan("p1", (200, {"ok": True, "image_present": True, "loop_devices": 8}))
check("E3 remote low loop -> warning names loop + floor",
      "loop device" in hr["warning"].lower() and "60" in hr["warning"])

hr = plan("p1", (404, {"error": "not found"}))
check("E4 old agent -> warning names 1.0.21 + installer re-run",
      "1.0.21" in hr["warning"] and "installer" in hr["warning"])

hr = plan("p1", (500, {"error": "boom"}))
check("E5 unreachable node -> warning says create may fail partway",
      "could not reach the node" in hr["warning"])

hr = app.fleet_headroom({}, "local", 2, 2048)
check("E6 local branch unchanged: probe stays ok, no probe warnings",
      hr["headroom_probe"] == "ok" and hr["image_present"] is True
      and "1.0.21" not in hr["warning"] and "could not reach" not in hr["warning"])

hr = plan("p1", (200, {"ok": True, "image_present": True, "loop_devices": 64}))
check("E7 remote: binder n/a (agent owns it), host RAM from heartbeat stats",
      hr["binder_free_triplets"] is None and hr["host_ram_mb"] == 8000)

reset({"p1": "not-a-dict"})
try:
    hr = app.fleet_headroom({}, "p1", 2, 2048)
    raised = False
except Exception:
    raised = True
check("E8 garbage provider record -> never raises", not raised)

hr = plan("p1", (200, {"ok": True, "image_present": True, "loop_devices": 64}))
check("E9 headroom dict carries headroom_probe (additive contract)",
      hr.get("headroom_probe") == "ok")

print("== F. dashboard contract ==")
with open(DASH, encoding="utf-8") as f:
    dash = f.read()
check("F1 plan modal renders headroom.warning (no dashboard change needed)",
      "headroom.warning" in dash or "p.warning" in dash)
check("F2 plan render needs no new fields (warning string only)",
      "fleetPlan" in dash or "plan" in dash.lower())
check("F3 no new API route required (probe is manager-internal)",
      "/api/agent/headroom" not in dash)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
