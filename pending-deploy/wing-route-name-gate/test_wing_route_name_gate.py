"""Unit tests for the wing agent route instance-name gate (agent 1.0.26).

Gap: the manager's route-name-gate (2026-10-19) fixed shell injection in
its own routes and claimed "the wing agent needs nothing" because
docker() is list-form and the marker readers fullmatch-reject. That claim
missed two sinks that build FILESYSTEM paths from the raw URL segment:

  GET  /api/agent/instances/<name>/screenshot -> screenshot_bytes_linux
  POST /api/agent/instances/<name>/input       -> input_linux -> _find_fifo
  POST /api/agent/instances/<name>/launch-roblox -> launch_roblox_linux

A name like a%2F..%2F..%2F..%2F..%2F..%2Fvar%2Flib%2Fdocker%2Fvolumes%2Fwd-rbx2
(percent-encoded slashes sail through the ([^/]+) route regex; unquote
runs after the match) resolved to ANOTHER instance's live.png / input.fifo:
cross-instance screen read + cross-instance input injection for anyone
holding the agent key hitting the agent directly (the manager path was
already gated). Authenticated-only, no RCE (docker() is list-form, fifo
writes are constrained `input ...` command strings) -- defense in depth
and audit correctness.

Fix: valid_agent_instance_name() (^[A-Za-z0-9_-]{1,32}$, the exact
create-time contract) gates all 8 instance-name route captures in
do_GET/do_POST/do_DELETE -> 404 "instance not found", indistinguishable
from an unknown instance; screenshot_bytes_linux/input_linux/
launch_roblox_linux refuse invalid names as defense in depth.

Covers: helper units (incl. never-raises on hostile input), the exact
exploit regression (traversal name -> sink refuses WITHOUT touching the
filesystem: os.path.exists / os.open monkeypatched to fail loudly if
called), source contracts that every route capture site carries the gate,
create-contract parity (create_instance_linux's own regex agrees), and
manager parity (the regex literal matches manager/app.py's
valid_instance_name).

Run: python3 test_wing_route_name_gate.py

Hermetic: wing/agent.py loaded via importlib; no network, no docker, no
real filesystem writes (os.open/os.path.exists monkeypatched in the
exploit tests).
"""
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("wingroutegate_agent", AGENT)
with open(AGENT) as f:
    AGENT_SRC = f.read()
with open(APP) as f:
    APP_SRC = f.read()

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


TRAVERSAL = "a/../../../../var/lib/docker/volumes/wd-rbx2"

# ---------------------------------------------------------------- version
# 1.0.26+: this suite owned the route-name instance gate at 1.0.26; the
# exact-bump assertion widens to 1.0.26+ now that node-caps-visibility owns
# exact 1.0.27 (handoff pattern).
check("VERSION is 1.0.26+", agent.VERSION in ("1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"))

# ---------------------------------------------------------------- helper units
g = agent.valid_agent_instance_name
check("valid: rbx1", g("rbx1") is True)
check("valid: srt-20", g("srt-20") is True)
check("valid: a_9-x", g("a_9-x") is True)
check("valid: 32 chars", g("a" * 32) is True)
check("invalid: empty", g("") is False)
check("invalid: None", g(None) is False)
check("invalid: non-string", g(["rbx1"]) is False)
check("invalid: 33 chars", g("a" * 33) is False)
check("invalid: slash", g("rbx1/x") is False)
check("invalid: dotdot", g("..") is False)
check("invalid: traversal", g(TRAVERSAL) is False)
check("invalid: semicolon", g("rbx1;id") is False)
check("invalid: space", g("rbx 1") is False)
check("invalid: unicode", g("rbx\u00e91") is False)
try:
    r = g(object())
    check("never-raises on hostile input", r is False)
except Exception:
    check("never-raises on hostile input", False)

# ------------------------------------------------- exploit regression (sinks)
# The traversal name must be refused BEFORE any filesystem access: patch
# os.path.exists / os.open to fail the test loudly if called.
_calls = []


def _boom_exists(p):
    _calls.append(("exists", p))
    raise AssertionError("filesystem touched for traversal name: %r" % p)


def _boom_open(*a, **k):
    _calls.append(("open", a))
    raise AssertionError("os.open touched for traversal name")


_real_exists, _real_open = os.path.exists, os.open
os.path.exists, os.open = _boom_exists, _boom_open
try:
    r1 = agent.screenshot_bytes_linux(TRAVERSAL)
    r2 = agent.input_linux(TRAVERSAL, "input tap 1 1")
    r3 = agent.launch_roblox_linux(TRAVERSAL)
finally:
    os.path.exists, os.open = _real_exists, _real_open
check("screenshot sink refuses traversal (None)", r1 is None)
check("input sink refuses traversal (False)", r2 is False)
check("launch sink refuses traversal (False)", r3 is False)
check("no filesystem access for traversal names", _calls == [])

# valid names still flow to the filesystem layer (behavior unchanged):
# screenshot on a missing instance returns None via the normal path
check("screenshot valid name, missing instance -> None",
      agent.screenshot_bytes_linux("definitely-not-an-instance-zz9") is None)

# ------------------------------------------------- route source contracts
# Every ([^/]+) instance route capture must compute name via unquote and
# carry the gate before any sink use. Extract each handler method source
# and check the gate text sits between the route regex and the method end.
def method_src(name):
    m = re.search(r"    def %s\(self.*?\n(?=    def |\nclass |\Z)" % name,
                  AGENT_SRC, re.S)
    assert m, "method %s not found" % name
    return m.group(0)


GATE = 'if not valid_agent_instance_name(name):'
for meth in ("do_GET", "do_POST", "do_DELETE"):
    src = method_src(meth)
    # count route captures that bind an instance name
    caps = re.findall(r're\.fullmatch\(r"/api/agent/instances/\(\[\^/\]\+\)[^"]*"',
                      src)
    # every capture's block must contain the gate after unquote
    blocks = re.findall(
        r'm = re\.fullmatch\(r"/api/agent/instances/\(\[\^/\]\+\)[^"]*".*?'
        r'(?=m = re\.fullmatch|self\._send\(404, \{"error": "not found"\}\))',
        src, re.S)
    ok = True
    for b in blocks:
        if "unquote(m.group(1))" not in b:
            continue  # no instance-name capture in this block
        if GATE not in b:
            ok = False
            break
        # gate must come before any sink call in the block
        gi = b.index(GATE)
        for sink in ("screenshot_bytes_linux(", "cached_instance_status_linux(",
                     "input_linux(", "launch_roblox_linux(",
                     "delete_instance_linux(", 'docker("'):
            si = b.find(sink)
            if si != -1 and si < gi:
                ok = False
                break
    check("%s: all %d instance captures gated before sinks" % (meth, len(caps)),
          ok and len(caps) > 0)

# the gate response is indistinguishable from an unknown instance
check("gate body matches unknown-instance 404",
      AGENT_SRC.count('{"error": "instance not found"}') >= 8)

# ------------------------------------------------- create-contract parity
# Names create_instance_linux accepts must pass the gate (no legit instance
# can 404); names it rejects must fail the gate too (same contract).
create_re = re.search(
    r'def create_instance_linux\(.*?\n(.*?)\n    cname',
    AGENT_SRC, re.S).group(1)
m = re.search(r're\.fullmatch\(r"([^"]+)"', create_re)
check("create regex extracted", m is not None)
if m:
    cre = m.group(1)
    agree = True
    for n in ["rbx1", "srt-20", "a_9-x", "a" * 32, "", "a" * 33,
              "rbx1;id", "..", TRAVERSAL, "rbx 1", None, ["x"]]:
        try:
            c_ok = bool(isinstance(n, str) and re.fullmatch(cre, n))
        except Exception:
            c_ok = False
        if c_ok != g(n):
            agree = False
            break
    check("gate agrees with create_instance_linux contract", agree)

# ------------------------------------------------- manager parity
# The agent gate must be the same contract as the manager's
# valid_instance_name (route-name-gate), so proxied names behave identically.
# The manager compiles it once as _INSTANCE_NAME_RE; compare pattern text.
mm = re.search(r'_INSTANCE_NAME_RE\s*=\s*re\.compile\(r"([^"]+)"\)', APP_SRC)
am = re.search(r"def valid_agent_instance_name\(.*?\n(?:.*\n)*?.*?re\.fullmatch\(r\"([^\"]+)\"",
               AGENT_SRC)
check("manager _INSTANCE_NAME_RE found", mm is not None)
check("agent valid_agent_instance_name found", am is not None)
if mm and am:
    check("agent gate regex == manager gate regex", mm.group(1) == am.group(1))

# ------------------------------------------------- no ungated sink regressions
# The three filesystem sinks must still contain their own gate (defense in
# depth), so a future caller can't reintroduce the traversal.
for fn in ("screenshot_bytes_linux", "input_linux", "launch_roblox_linux"):
    m = re.search(r"def %s\(.*?\n(.*?)\n    cname" % fn, AGENT_SRC, re.S)
    check("%s keeps its own sink gate" % fn,
          m is not None and "valid_agent_instance_name(name)" in m.group(1))

print(f"\n{ PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
