"""Unit tests for the single-instance restart route (manager/app.py +
dashboard card button).

Gap: instance_single_action() has supported "restart" (stop-then-start,
local + remote) since batch-ops, and the batch worker uses it — but the
per-instance HTTP route only matched (start|stop), so POST
/api/instances/<name>/restart fell through to the do_POST 404. The
dashboard never sent it (cards had no Restart button), so the hole was
latent, not user-visible — the same class as read-body-missing (a route
the core supports but the handler never exposes).

Run: python3 test_single_restart_route.py

Hermetic: the real app.py module is loaded; instance_single_action is
monkeypatched for the dispatch tests (the core itself is covered by
batch-ops); no docker, no network. The dashboard is read as text.
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

app = load("restart_route_app", os.path.join(HERE, "..", "..", "manager", "app.py"))

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")

APP_SRC = open(os.path.join(HERE, "..", "..", "manager", "app.py")).read()
POST_SRC = APP_SRC[APP_SRC.index("    def do_POST(self):"):]
HTML = open(os.path.join(HERE, "..", "..", "manager", "static", "index.html")).read()

# ---------------- live route regex (extracted, never a literal) ----------------
m = re.search(r"r\"/api/instances/\(\[\^/\]\+\)/\(([^)]+)\)\"", POST_SRC)
check("single-action route regex found in live do_POST source", m is not None)
ROUTE = re.compile(r"/api/instances/([^/]+)/(" + m.group(1) + r")") if m else None

print("== live route regex ==")
if ROUTE:
    check("route accepts restart", ROUTE.fullmatch("/api/instances/rbx1/restart") is not None
          and ROUTE.fullmatch("/api/instances/rbx1/restart").group(2) == "restart")
    check("route still accepts start", ROUTE.fullmatch("/api/instances/rbx1/start").group(2) == "start")
    check("route still accepts stop", ROUTE.fullmatch("/api/instances/rbx1/stop").group(2) == "stop")
    check("route rejects unknown action", ROUTE.fullmatch("/api/instances/rbx1/explode") is None)
    check("route does not swallow /api/instances/batch",
          ROUTE.fullmatch("/api/instances/batch") is None)
    check("route does not swallow /api/instances/fleet/plan",
          ROUTE.fullmatch("/api/instances/fleet/plan") is None)
    check("route does not swallow /api/instances/fleet",
          ROUTE.fullmatch("/api/instances/fleet") is None)
    check("route captures the instance name",
          ROUTE.fullmatch("/api/instances/srt1/restart").group(1) == "srt1")

print("== route placement in do_POST ==")
act_idx = POST_SRC.index(m.group(0)) if m else -1
check("batch exact route precedes the action route",
      POST_SRC.index('"/api/instances/batch"') < act_idx)
check("fleet/plan exact route precedes the action route",
      POST_SRC.index('"/api/instances/fleet/plan"') < act_idx)
check("fleet exact route precedes the action route",
      POST_SRC.index('"/api/instances/fleet"') < act_idx)

# ---------------- real dispatch through Handler.do_POST ----------------
CALLS = []
CAPTURED = {}

def fake_single_action(name, action):
    CALLS.append((name, action))
    return 200, {"ok": True}

def make_handler(path, authed=True):
    h = object.__new__(app.Handler)
    h.path = path
    h.command = "POST"
    h.headers = {}
    h.client_address = ("127.0.0.1", 1234)
    h._authed = lambda: authed
    def _json(code, obj):
        CAPTURED["code"] = code
        CAPTURED["obj"] = obj
    h._json = _json
    return h

app.instance_single_action = fake_single_action

def dispatch(path, authed=True):
    CALLS.clear()
    CAPTURED.clear()
    make_handler(path, authed).do_POST()
    return dict(CAPTURED), list(CALLS)

print("== dispatch ==")
cap, calls = dispatch("/api/instances/rbx1/restart")
check("POST .../restart dispatches to the core with action restart",
      cap.get("code") == 200 and calls == [("rbx1", "restart")])
cap, calls = dispatch("/api/instances/rbx1/start")
check("POST .../start still dispatches",
      cap.get("code") == 200 and calls == [("rbx1", "start")])
cap, calls = dispatch("/api/instances/rbx1/stop")
check("POST .../stop still dispatches",
      cap.get("code") == 200 and calls == [("rbx1", "stop")])
cap, calls = dispatch("/api/instances/rbx1/explode")
check("POST .../explode -> 404, core never called",
      cap.get("code") == 404 and calls == [])
cap, calls = dispatch("/api/instances/../restart")
check("POST with invalid name -> 404, core never called",
      cap.get("code") == 404 and calls == [])
cap, calls = dispatch("/api/instances/rbx1/restart", authed=False)
check("unauthed POST .../restart -> 401, core never called",
      cap.get("code") == 401 and calls == [])

# ---------------- core meaning of the routed action (regression pin) ----------------
RUNS = []
WINGS = []
INSTANCES = {"rbx1": {"provider": "local"}, "srt1": {"provider": "p_vultr"}}
PROVIDERS = {"p_vultr": {"url": "http://10.0.0.2:8090", "agent_key": "k"}}
# Reload a fresh module copy for the real core (app was monkeypatched above
# for the dispatch tests); the core itself is covered by batch-ops, this
# only pins what the newly exposed route means.
real_mod = load("restart_route_real", os.path.join(HERE, "..", "..", "manager", "app.py"))
real_mod.load_instances = lambda: dict(INSTANCES)
real_mod.load_providers = lambda: dict(PROVIDERS)
real_mod.run = lambda cmd, timeout=30: (RUNS.append(cmd), (0, "", ""))[1]
real_mod.wing_request = lambda p, method, path, data=None, timeout=8: (
    WINGS.append((method, path, timeout)), (200, {"ok": True}))[1]
real_mod.container_status = lambda c: "running"
real_mod._prepare_pools_for_start = lambda: True

print("== core restart behind the route ==")
RUNS.clear()
code, _ = real_mod.instance_single_action("rbx1", "restart")
check("local restart -> docker stop then start, in order",
      code == 200 and RUNS == ["docker stop wd-rbx1", "docker start wd-rbx1"])
WINGS.clear()
code, _ = real_mod.instance_single_action("srt1", "restart")
check("remote restart -> wing stop then wing start",
      code == 200 and WINGS == [("POST", "/api/agent/instances/srt1/stop", 60),
                                ("POST", "/api/agent/instances/srt1/start", 60)])

# ---------------- dashboard contract ----------------
print("== dashboard contract ==")
check("card has a Restart button", 'data-act="restart"' in HTML)
check("Restart button labeled Reiniciar", "Reiniciar</button>" in HTML)
check("restart asks for confirmation", 'a===\'restart\' && !confirm(' in HTML)
check("start button intact", 'data-act="start"' in HTML)
check("stop button intact", 'data-act="stop"' in HTML)
check("resize button intact", 'data-act="resize"' in HTML)
check("delete button intact", 'data-act="del"' in HTML)
check("view button intact", 'data-act="view"' in HTML)
check("act() still posts generic actions to /api/instances/<name>/<action>",
      "api(`/api/instances/${name}/${a}`" in HTML)
check("new strings are Spanish (no English Restart label)",
      ">Restart<" not in HTML and "Reiniciar" in HTML)

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
