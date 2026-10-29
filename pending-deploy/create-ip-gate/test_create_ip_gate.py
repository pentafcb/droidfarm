"""Unit tests for the create-IP gate (manager/app.py).

Run: python3 test_create_ip_gate.py

The gap: POST /api/instances passes data.get("ip") straight into
create_local_instance(), which interpolated it into a shell=True
`docker run` command line with NO validation. An authenticated dashboard
user could send {"ip": "10.20.0.5;touch${IFS}/tmp/x"} and get root shell
execution on the manager host — the same severity class as the
route-name-gate work (which covered name captures but missed ip).

The fix: valid_ipv4() (strict dotted-quad IPv4 literal via stdlib
ipaddress) gates create_local_instance's shared core (single-create +
fleet worker): invalid -> 400 + reservation rollback, before any docker
call. next_free_ip() only yields dotted quads, so the gate never fires
on the generated-IP path.

Hermetic: imports the real manager/app.py; docker/instances/ip/loop
helpers are faked in-memory. No docker, no network, no disk writes.
"""
import importlib.util
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

app = load("cipg_app", os.path.join(HERE, "..", "..", "manager", "app.py"))

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")

# ---------------- valid_ipv4: units ----------------
for good in ["10.20.0.11", "192.168.1.1", "1.2.3.4", "255.255.255.255",
             "0.0.0.0", "10.0.0.1"]:
    check(f"valid_ipv4 accepts {good!r}", app.valid_ipv4(good) is True)

# interior whitespace / malformed / non-literals
check("valid_ipv4 rejects empty", app.valid_ipv4("") is False)
check("valid_ipv4 rejects None", app.valid_ipv4(None) is False)
check("valid_ipv4 rejects hostname", app.valid_ipv4("hostname") is False)
check("valid_ipv4 rejects octet >255", app.valid_ipv4("10.20.0.256") is False)
check("valid_ipv4 rejects short quad", app.valid_ipv4("1.2.3") is False)
check("valid_ipv4 rejects long quad", app.valid_ipv4("1.2.3.4.5") is False)
check("valid_ipv4 rejects IPv6", app.valid_ipv4("::1") is False)
check("valid_ipv4 rejects CIDR", app.valid_ipv4("10.20.0.1/24") is False)
check("valid_ipv4 rejects leading zeros", app.valid_ipv4("010.20.0.1") is False)
check("valid_ipv4 rejects interior space", app.valid_ipv4("10.20 .0.1") is False)
# surrounding whitespace is stripped by both the caller and the helper;
# a stripped valid quad is harmless in the shell string.
check("valid_ipv4 strips padding (safe)", app.valid_ipv4("  10.20.0.1  ") is True)

# hostile: shell metacharacters must never validate
HOSTILE = [
    "10.20.0.5;touch${IFS}/tmp/x",
    "10.20.0.5$(id)",
    "10.20.0.5`id`",
    "10.20.0.5|id",
    "10.20.0.5&&id",
    "10.20.0.5||id",
    "10.20.0.5\ntouch /tmp/x",
    "10.20.0.5\rtouch /tmp/x",
    "'10.20.0.5'",
    '"10.20.0.5"',
    "10.20.0.5#comment",
    "${IFS}10.20.0.5",
    "10.20.0.5>",
    "<10.20.0.5",
]
for h in HOSTILE:
    check(f"valid_ipv4 rejects hostile {h[:24]!r}", app.valid_ipv4(h) is False)

# non-string / hostile objects: never raise, never validate
for weird in [12345, 10.5, ["10.20.0.1"], {"a": 1}, ("10.20.0.1",),
              object(), True, b"10.20.0.1"]:
    try:
        r = app.valid_ipv4(weird)
        check(f"valid_ipv4 never-raises on {type(weird).__name__}",
              r is False)
    except Exception:
        check(f"valid_ipv4 never-raises on {type(weird).__name__}", False)

# ---------------- create_local_instance: wiring ----------------
_state = {"instances": {}, "run_calls": []}

def fake_load_instances():
    return dict(_state["instances"])

def fake_save_instances(insts):
    _state["instances"] = dict(insts)

def reset():
    _state["instances"] = {}
    _state["run_calls"] = []
    app.load_instances = fake_load_instances
    app.save_instances = fake_save_instances
    app.docker_inspect = lambda cname: None
    app.next_free_ip = lambda insts: "10.20.0.99"
    app.free_binder_triplet = lambda used: ("binder", "binder1", "binder2")
    app.used_binders = lambda insts: set()
    app.ensure_binder_devices = lambda: True
    app.waydroid_image_present = lambda: True
    app.ensure_loop_devices = lambda: True
    app.count_loop_devices = lambda: 64
    app.next_instance_id = lambda insts: 5
    def fake_run(cmd, timeout=None):
        _state["run_calls"].append(cmd)
        return (0, "", "")
    app.run = fake_run

reset()

def create(name, ip):
    return app.create_local_instance(name, 2.0, 2048, ip=ip)

# hostile ip -> 400, reservation rolled back, docker NEVER invoked
for h in HOSTILE:
    reset()
    code, resp = create("evil1", h)
    check(f"hostile ip {h[:20]!r} -> 400",
          code == 400 and "invalid IP" in str(resp.get("error", "")))
    check(f"hostile ip {h[:20]!r} rolled back",
          "evil1" not in _state["instances"])
    check(f"hostile ip {h[:20]!r} never reached docker",
          _state["run_calls"] == [])

# plain garbage ip -> same fail-fast
reset()
code, resp = create("evil2", "not-an-ip")
check("garbage ip -> 400 + rollback + no docker",
      code == 400 and "evil2" not in _state["instances"]
      and _state["run_calls"] == [])

# valid explicit ip -> proceeds, ip interpolated verbatim into the command
reset()
code, resp = create("good1", "10.20.0.50")
check("valid ip -> 200", code == 200 and resp.get("ok") is True)
check("valid ip recorded", _state["instances"]["good1"]["ip"] == "10.20.0.50")
check("docker run got the ip",
      len(_state["run_calls"]) == 1
      and "--ip 10.20.0.50" in _state["run_calls"][0])
check("docker run carries cpus/memory",
      "--cpus 2.0" in _state["run_calls"][0]
      and "--memory 2048m" in _state["run_calls"][0])

# ip=None / "" -> generated path, gate never fires on next_free_ip output
for ipval in (None, ""):
    reset()
    code, resp = create("gen1", ipval)
    check(f"ip={ipval!r} -> generated 10.20.0.99",
          code == 200 and "--ip 10.20.0.99" in _state["run_calls"][0])

# exploit regression proof: the payload must not appear in ANY shell string
reset()
payload = "10.20.0.5;touch${IFS}/tmp/pwned"
code, _ = create("evil3", payload)
joined = "\n".join(_state["run_calls"])
check("payload never reaches a shell string",
      code == 400 and "touch" not in joined and _state["run_calls"] == [])

# ---------------- source contracts ----------------
src = inspect.getsource(app.create_local_instance)
resv = src.find('instances[name] = {')
gate = src.find("valid_ipv4(ip)")
dockerrun = src.find("docker run -d")
check("gate: reservation before gate", 0 <= resv < gate)
check("gate: gate before docker run string", 0 <= gate < dockerrun)
check("gate: rollback present", "instances.pop(name, None)" in src)
check("gate: 400 on invalid", '"invalid IP' in src)

# deadlock regression: the gate's rollback `with _lock:` must NOT be nested
# inside the first `with _lock:` block (_lock is a non-reentrant
# threading.Lock; nesting deadlocks the create call). The gate `if` sits at
# function-body indent (4 spaces), so its `with _lock:` is at 8 spaces —
# exactly one level, never nested inside the reservation block's lock.
glines = src.splitlines()
gidx = next(i for i, l in enumerate(glines) if "valid_ipv4(ip)" in l)
check("gate: if at body indent", glines[gidx].startswith("    if "))
nested = [l for l in glines[gidx:gidx + 12]
          if l.strip() == "with _lock:"]
check("gate: rollback lock exactly one level deep (no nested deadlock)",
      nested == ["        with _lock:"])

vsrc = inspect.getsource(app.valid_ipv4)
check("valid_ipv4: stdlib ipaddress", "IPv4Address" in vsrc)
check("valid_ipv4: never-raises", "except Exception" in vsrc)

# the route still passes ip through to the shared core (no behavior change
# for valid ips; the core owns the gate)
post_src = inspect.getsource(app.Handler._create_instance)
check("route: ip still passed to core",
      "create_local_instance(name, cpus, memory_mb," in post_src
      and 'ip=data.get("ip")' in post_src)

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
