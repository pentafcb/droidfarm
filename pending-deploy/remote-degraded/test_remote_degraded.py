"""Unit tests for remote per-instance degraded surfacing (wing agent 1.0.22).

Gap: the wing agent's selfwatch (1.0.3+) writes per-instance <name>.degraded
markers when its circuit breaker pauses auto-recovery, but that state never
reached the dashboard's instance cards. The manager's /api/instances remote
branch hardcoded "degraded": False with the stale comment "watchdog only
runs on the main node" — a wedged remote instance (restart storm, helper
crash loop, session-death storm) kept showing a green "Ready" card. The
twelfth "main had it, wing didn't" parity item (1.0.5 drift, 1.0.6
adaptive-screencap, 1.0.7 disk/log, 1.0.11 session-death, 1.0.12 loop
devices, 1.0.13 memory, 1.0.14 stuck-boot, 1.0.15 keepalive mirror, 1.0.16
binderfs self-heal, 1.0.17 agent HTTP self-check, 1.0.18 create image
preflight, 1.0.19 auth rate-limit, 1.0.20 status cache, 1.0.21 headroom
probe).

Covers: agent 1.0.22 read_selfwatch_degraded() units, instance_status_linux
both branches, the 1.0.20 status-cache passthrough, manager
clean_agent_degraded/_reason sanitizers, the /api/instances remote-branch
source contract (old agents omit the fields -> False/None), and the
dashboard contract (no dashboard change needed — the card badge already
reads i.degraded with priority over the setup/drift badges).

Run: python3 test_remote_degraded.py

Hermetic: fake st_dir + patched container_status/instance_resources_all/
docker; the manager branch is asserted as a source contract like the
remote-resources suite does.
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")
HTML = os.path.join(REPO, "manager", "static", "index.html")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("rd_agent", AGENT)
app = load("rd_app", APP)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")


# ---- A. version ----
print("== A. version ==")
check("agent VERSION is 1.0.22+ (1.0.23 adds selfwatch single-instance lock)",
      agent.VERSION in ("1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"), repr(agent.VERSION))
check("manager >=1.0.2 gate accepts 1.0.22",
      app._version_at_least("1.0.22", "1.0.2"))
check("manager gate still rejects old agents",
      not app._version_at_least("1.0.21", "1.0.22")
      and app._version_at_least("1.0.21", "1.0.2"))

# ---- B. read_selfwatch_degraded units ----
print("== B. read_selfwatch_degraded ==")
tmp = tempfile.mkdtemp(prefix="rd-deg-")


def wmarker(name, content):
    with open(os.path.join(tmp, name + ".degraded"), "w") as fh:
        fh.write(content)


wmarker("srt1", "screencap-loop restart storm\nsecond line ignored\n")
check("marker with reason -> (True, first line)",
      agent.read_selfwatch_degraded("srt1", tmp) == (True, "screencap-loop restart storm"))
wmarker("srt2", "\n")
check("empty marker -> (True, default reason)",
      agent.read_selfwatch_degraded("srt2", tmp) == (True, "auto-recovery paused"))
check("missing marker -> (False, None)",
      agent.read_selfwatch_degraded("nope", tmp) == (False, None))
wmarker("srt3", "x" * 200)
d, r = agent.read_selfwatch_degraded("srt3", tmp)
check("reason truncated to 160", d is True and r == "x" * 160, repr(r)[:40])
for hostile in ["../evil", "a/b", "", "x" * 33, "srt1\ninjected", None]:
    d, r = agent.read_selfwatch_degraded(hostile, tmp)
    check(f"hostile name {hostile!r} rejected", (d, r) == (False, None))
check("unreadable dir -> (False, None), never raises",
      agent.read_selfwatch_degraded("srt1", "/nonexistent-rd-dir") == (False, None))
# traversal must not escape st_dir
evil = os.path.join(tmp, "evil.degraded")
if os.path.exists(evil):
    os.unlink(evil)
agent.read_selfwatch_degraded("../../evil", tmp)
check("no marker file written outside st_dir", not os.path.exists(evil))

# ---- C. instance_status_linux branches ----
print("== C. instance_status_linux ==")
old_cs = agent.container_status
old_res = agent.instance_resources_all
old_drift = agent.read_roblox_drift
old_docker = agent.docker
old_swdir = agent.SELF_WATCH_DIR
try:
    agent.container_status = lambda cname: "running"
    agent.instance_resources_all = lambda: {}
    agent.read_roblox_drift = lambda n: (False, None)
    agent.docker = lambda *a, **k: (1, "")  # inspect fails -> started_at 0
    agent.SELF_WATCH_DIR = tmp
    s = agent.instance_status_linux("srt1")
    check("running branch has degraded fields",
          s.get("degraded") is True and s.get("degraded_reason") == "screencap-loop restart storm",
          repr({k: s.get(k) for k in ("degraded", "degraded_reason")}))
    s2 = agent.instance_status_linux("nope")
    check("running, no marker -> (False, None)",
          s2.get("degraded") is False and s2.get("degraded_reason") is None)
    agent.container_status = lambda cname: "exited"
    s3 = agent.instance_status_linux("srt1")
    check("stopped branch has degraded fields, False/None",
          s3.get("degraded") is False and s3.get("degraded_reason") is None
          and s3.get("running") is False)
    check("stopped branch keeps roblox_drift contract",
          s3.get("roblox_drift") is False and s3.get("roblox_drift_reason") is None)
finally:
    agent.container_status = old_cs
    agent.instance_resources_all = old_res
    agent.read_roblox_drift = old_drift
    agent.docker = old_docker
    agent.SELF_WATCH_DIR = old_swdir

# ---- D. manager sanitizers ----
print("== D. manager sanitizers ==")
check("clean_agent_degraded(True) is True", app.clean_agent_degraded(True) is True)
for v in ["true", 1, None, 0, "", [], {}]:
    check(f"clean_agent_degraded({v!r}) is False", app.clean_agent_degraded(v) is False)
check("reason passthrough + strip",
      app.clean_agent_degraded_reason("  boom  ") == "boom")
check("reason truncated to 160",
      app.clean_agent_degraded_reason("y" * 200) == "y" * 160)
for v in [None, "", "   ", 123, [], {}]:
    check(f"clean_agent_degraded_reason({v!r}) is None",
          app.clean_agent_degraded_reason(v) is None)
try:
    app.clean_agent_degraded(object())
    app.clean_agent_degraded_reason(object())
    check("sanitizers never raise on garbage", True)
except Exception as e:  # noqa: BLE001
    check("sanitizers never raise on garbage", False, repr(e))

# ---- E. /api/instances remote-branch source contract ----
print("== E. remote-branch contract ==")
src = open(APP).read()
api_inst = src.find('if path == "/api/instances":')
check("/api/instances handler found", api_inst > 0)
tail = src[api_inst:] if api_inst > 0 else ""
check("remote degraded via clean_agent_degraded",
      '"degraded": clean_agent_degraded(rs.get("degraded"))' in tail)
check("remote degraded_reason via clean_agent_degraded_reason",
      'clean_agent_degraded_reason(' in tail and 'rs.get("degraded_reason")' in tail)
inst_end = tail.find('m = re.fullmatch(r"/api/instances/([^/]+)/roblox"')
body = tail[:inst_end] if inst_end > 0 else tail
check("no hardcoded degraded False pair left in /api/instances",
      '"degraded": False, "degraded_reason": None' not in body)
check("stale 'watchdog only runs on the main node' comment gone",
      "watchdog only runs on the main node" not in src)

# ---- F. dashboard contract (no dashboard change) ----
print("== F. dashboard contract ==")
html = open(HTML).read()
check("card badge reads i.degraded", "if (i.degraded) setupBadge" in html)
check("badge shows reason in tooltip", "esc(i.degraded_reason||'')" in html)
check("degraded badge has priority over drift badge",
      html.find("if (i.degraded)") < html.find("else if (i.roblox_drift)"))
check("auto-refresh covers degraded cards", "|| i.degraded" in html)

# ---- G. status-cache passthrough ----
print("== G. status-cache passthrough ==")
asrc = open(AGENT).read()
check("cached wrapper delegates to instance_status_linux",
      "cached_instance_status_linux" in asrc and "instance_status_linux(name)" in asrc)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
