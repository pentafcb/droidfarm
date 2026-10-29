"""Unit tests for the agent-side IP gate (wing/agent.py).

Gap: POST /api/agent/instances passed data.get("ip") straight into
create_instance_linux(), which handed it verbatim to `docker run --ip`
with NO validation — name was fullmatch-gated, ip was not. The agent's
docker() is list-form (no shell), so a hostile ip could never inject a
command here, but it failed cryptically deep in docker run, and any
future shell-form sink would have inherited the hole. This is the
agent-side parity of the manager's create-ip-gate.

Fix: valid_agent_ipv4() (strict dotted-quad via stdlib ipaddress, never
raises, no stripping — the validated value must be exactly the value
docker receives) gates create_instance_linux before any docker call:
invalid ip -> (False, "invalid ip"), docker never invoked. Valid-input
behavior is unchanged, so no agent version bump: the manager's login
gate (>= 1.0.38) and every VERSION-membership suite are unaffected.

Hermetic: imports the real wing/agent.py; docker/container-status/
image/binder/instances helpers are faked in-memory. No docker, no
network, no disk writes outside a temp dir.

Run: python3 test_agent_ip_gate.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("aipg_agent", os.path.join(REPO, "wing", "agent.py"))

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


# ---------------- valid_agent_ipv4: units ----------------
for good in ["10.20.0.11", "192.168.1.1", "1.2.3.4", "255.255.255.255",
             "0.0.0.0", "10.20.0.249"]:
    check(f"valid_agent_ipv4 accepts {good!r}",
          agent.valid_agent_ipv4(good) is True)

HOSTILE = [
    "", None, 123, ["10.0.0.1"],
    "hostname", "10.20.0.256", "1.2.3", "1.2.3.4.5", "::1",
    "10.20.0.1/24", "10.20.0.1/32",
    "10.20.0.5;touch${IFS}/tmp/x", "10.20.0.5$(id)", "10.20.0.5`id`",
    "10.20.0.5|cat /etc/passwd", "10.20.0.5\n10.20.0.6",
    " 10.20.0.5", "10.20.0.5 ", "10.20.0.5\t",
    "--privileged", "-e", "10.20.0.5 --privileged",
    "010.20.0.5", "10.20.0.05",
]
for bad in HOSTILE:
    check(f"valid_agent_ipv4 rejects {bad!r}",
          agent.valid_agent_ipv4(bad) is False)

# never raises on hostile types
for weird in [object(), b"10.0.0.1", {"ip": "1.2.3.4"}]:
    try:
        r = agent.valid_agent_ipv4(weird)
        check(f"valid_agent_ipv4 never raises on {type(weird).__name__}",
              r is False)
    except Exception:
        check(f"valid_agent_ipv4 never raises on {type(weird).__name__}", False)


# ---------------- create_instance_linux: gate behavior ----------------
# Fake the agent's docker-adjacent helpers in-memory.
calls = {"docker": [], "saved": None}
def _fake_docker(*a, **k):
    calls["docker"].append(list(a))
    return 0, "ok"


agent.docker = _fake_docker
agent.container_status = lambda cname: "missing"
agent.sw_image_present = lambda: True
agent.sw_find_binder_triplet = lambda: ("/dev/binderfs/binder",
                                        "/dev/binderfs/binder1",
                                        "/dev/binderfs/binder2")
agent.sw_ensure_loop_devices = lambda: True
agent.load_instances = lambda: {}
agent.save_instances = lambda d: calls.__setitem__("saved", d)
agent.NETWORK = "droid-net"


def run_create(name, ip):
    calls["docker"] = []
    calls["saved"] = None
    return agent.create_instance_linux(name, 2, 2048, ip)


# Hostile ip: rejected before docker, exact message, nothing saved.
for bad in ["10.20.0.5;touch${IFS}/tmp/x", "not-an-ip", "10.20.0.1/24",
            "", None, "::1", "10.20.0.5 --privileged"]:
    ok, msg = run_create("srt9", bad)
    check(f"create rejects ip {bad!r} with 'invalid ip'",
          ok is False and msg == "invalid ip")
    check(f"create with ip {bad!r} never calls docker",
          calls["docker"] == [])
    check(f"create with ip {bad!r} saves nothing",
          calls["saved"] is None)

# Name gate still fires first (precedence unchanged).
ok, msg = run_create("bad name!", "10.20.0.5;id")
check("invalid name still reported before invalid ip",
      ok is False and msg == "invalid name")

# Valid ip: proceeds to docker run with the exact ip as its own argv.
ok, msg = run_create("srt9", "10.20.0.55")
check("create accepts valid ip", ok is True and msg == "created")
run_cmd = next((c for c in calls["docker"] if c and c[0] == "run"), [])
check("docker run carries --ip as its own argv",
      "--ip" in run_cmd and run_cmd[run_cmd.index("--ip") + 1] == "10.20.0.55")
check("docker run argv has no shell metachar ip fragment",
      all(";" not in str(a) and "$(" not in str(a) for a in run_cmd))
check("valid create saves instance with exact ip",
      calls["saved"] is not None
      and calls["saved"].get("srt9", {}).get("ip") == "10.20.0.55")

# Manager-generated remote-create range passes the gate.
for n in (11, 100, 249):
    ok, _ = run_create("srt9", f"10.20.0.{n}")
    check(f"manager-generated ip 10.20.0.{n} passes", ok is True)

# Already-exists path still short-circuits before docker run.
agent.container_status = lambda cname: "running"
ok, msg = run_create("srt9", "10.20.0.55")
check("existing container still reports 'already exists'",
      ok is False and msg == "already exists")
check("already-exists path never calls docker", calls["docker"] == [])

# Source contract: gate sits before the docker run construction.
src = open(os.path.join(REPO, "wing", "agent.py")).read()
gate_pos = src.find('if not valid_agent_ipv4(ip):')
run_pos = src.find('cmd = [', src.find('def create_instance_linux'))
check("source: ip gate precedes docker run construction",
      0 < gate_pos < run_pos)
check("source: handler still delegates to create_instance_linux",
      "create_instance_linux(name, cpus, memory_mb, ip)" in src)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
