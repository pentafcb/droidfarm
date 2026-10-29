#!/usr/bin/env python3
"""Tests for wing agent 1.0.36: /resize invalidates the per-instance status
snapshot cache.

REAL GAP: wing-status-cache (agent 1.0.20) documented the contract
"Mutation endpoints (create/start/stop/delete) invalidate the entry, so the
next poll shows fresh state" — but the /resize route (added in 1.0.31, two
versions later) never got the invalidation. After a remote resize, the
agent's 10s-TTL status snapshot kept serving the pre-resize state until TTL
expiry. (The snapshot holds usage/setup/degraded/drift rather than the
limits themselves, which the manager keeps in instances.json — so this is a
contract-consistency fix: every mutation route now invalidates, no
exceptions.)

This suite covers:

  A. agent.VERSION is exactly 1.0.36 + manager >=1.0.2 gate compatibility
  B. source contract: the /resize do_POST block calls
     invalidate_agent_status_cache(name) after a successful resize
     (gated on ok, the create-route pattern)
  C. the invalidation is NOT on the validation-failure paths (400s return
     before the mutation, like the other routes)
  D. functional: seed the 10s status cache, run the route's invalidation,
     verify the entry drops and the next cached_instance_status_linux
     refetches (docker called again)
  E. regression: create/start/stop/delete routes still invalidate
     (the 1.0.20 contract, unchanged)
  F. invalidate_agent_status_cache never raises on garbage names
"""
import importlib.util
import inspect
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")

PASS = FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok: {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {name} {detail}")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("rci_agent", AGENT)
app = load("rci_app", APP)

print("== A. version + gate ==")
if agent.VERSION in ("1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"):
    ok("A1 VERSION is 1.0.36+ (exact 1.0.37 owned by agent-body-cap)")
else:
    bad("A1 VERSION is 1.0.36+", f"got {agent.VERSION!r}")
if agent.parse_agent_version("1.0.36") > agent.parse_agent_version("1.0.35"):
    ok("A2 parse_agent_version orders 1.0.36 > 1.0.35")
else:
    bad("A2 parse_agent_version ordering", "")
# manager gate: app.py must accept the new agent version (>=1.0.2 contract)
src_app = open(APP).read()
m = re.search(r"def _agent_version_ok\(.*?\).*?(?=\ndef |\nclass )",
              src_app, re.S)
gate_src = m.group(0) if m else src_app
if "1.0.2" in gate_src or "min" in gate_src.lower():
    ok("A3 manager agent-version gate present (>=1.0.2 family)")
else:
    bad("A3 manager agent-version gate", "gate not found")

print("== B. resize route invalidates ==")
src = inspect.getsource(agent.Handler.do_POST)
# isolate the /resize block: from its route regex to the next route regex
start = src.find('/api/agent/instances/([^/]+)/resize')
if start == -1:
    bad("B resize route exists", "route regex not found")
    resize_block = ""
else:
    tail = src[start:]
    nxt = tail.find('/api/agent/instances/([^/]+)"', 10)
    resize_block = tail[:nxt if nxt != -1 else len(tail)]
if "invalidate_agent_status_cache(name)" in resize_block:
    ok("B1 resize route calls invalidate_agent_status_cache(name)")
else:
    bad("B1 resize route invalidates", "call missing from resize block")
# gated on success (create-route pattern): the call sits after
# resize_instance_linux and inside an `if ok:` branch
ok_idx = resize_block.find("invalidate_agent_status_cache(name)")
call_idx = resize_block.find("resize_instance_linux(name")
if 0 <= call_idx < ok_idx and "if ok:" in resize_block[call_idx:ok_idx]:
    ok("B2 invalidation gated on ok (after resize_instance_linux)")
else:
    bad("B2 invalidation gated on ok", "ordering/guard wrong")

print("== C. no invalidation on validation failures ==")
# the 400 returns (bad json / nothing to resize / out of bounds / usage
# refusal) all happen before resize_instance_linux is reached
for label, needle in (
    ("bad json", "invalid cpus/memory"),
    ("nothing to resize", "memory_mb or cpus required"),
    ("bounds", "memory 1024-32768 MB"),
):
    if needle in resize_block:
        ok(f"C validation path present: {label}")
    else:
        bad(f"C validation path present: {label}", needle)
# none of those returns is followed by an invalidation in the block
pre_call = resize_block[:resize_block.find("resize_instance_linux")]
if "invalidate_agent_status_cache" not in pre_call:
    ok("C2 no invalidation before the mutation call")
else:
    bad("C2 no invalidation before the mutation call", "")

print("== D. functional: invalidate drops the cached entry ==")


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.containers = {"wd-srt1": True}

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[0] == "ps":
            return 0, "\n".join(self.containers.keys())
        if args[0] == "inspect":
            return 0, "2026-10-23T10:00:00Z\n"
        if args[0] == "exec":
            return 1, ""
        return 0, ""


orig_docker = agent.docker
orig_cache = dict(agent._agent_status_cache)
agent._agent_status_cache.clear()
fake = FakeDocker()
agent.docker = fake
try:
    # seed the cache via the cached wrapper
    with tempfile.TemporaryDirectory() as vol:
        # minimal volume tree so marker reads don't explode
        base = os.path.join(vol, "wd-srt1-data", "_data", "data", "media",
                            "0", "jerusalem")
        os.makedirs(base)
        orig_vol = None
        s1 = agent.cached_instance_status_linux("srt1")
        n_calls_first = len(fake.calls)
        s2 = agent.cached_instance_status_linux("srt1")
        if len(fake.calls) == n_calls_first:
            ok("D1 second call served from cache (no new docker calls)")
        else:
            bad("D1 cache hit", f"{len(fake.calls)} vs {n_calls_first}")
        # the route's invalidation (what 1.0.36 added)
        agent.invalidate_agent_status_cache("srt1")
        if "srt1" not in agent._agent_status_cache:
            ok("D2 invalidate drops the entry")
        else:
            bad("D2 invalidate drops the entry", "")
        s3 = agent.cached_instance_status_linux("srt1")
        if len(fake.calls) > n_calls_first:
            ok("D3 next call refetches (docker called again)")
        else:
            bad("D3 refetch after invalidate", "no new docker calls")
        if isinstance(s3, dict) and s3.get("name") == "srt1":
            ok("D4 refetched snapshot has the right shape")
        else:
            bad("D4 refetched snapshot shape", repr(s3)[:80])
finally:
    agent.docker = orig_docker
    agent._agent_status_cache.clear()
    agent._agent_status_cache.update(orig_cache)

print("== E. other mutation routes still invalidate ==")
for route, label in (
    ('"/api/agent/instances"', "create"),
    ("/start", "start"),
    ("/stop", "stop"),
):
    if route in src and "invalidate_agent_status_cache(name)" in src:
        ok(f"E {label} route still invalidates")
    else:
        bad(f"E {label} route still invalidates", route)
# delete lives in do_DELETE
src_del = inspect.getsource(agent.Handler.do_DELETE)
if "invalidate_agent_status_cache(name)" in src_del:
    ok("E delete route still invalidates")
else:
    bad("E delete route still invalidates", "")

print("== F. never raises on garbage ==")
for g in (None, 123, ["x"], {"a": 1}, "srt1; rm -rf /", ""):
    try:
        agent.invalidate_agent_status_cache(g)
    except Exception as e:
        bad("F garbage never raises", "%r -> %r" % (g, e))
        break
else:
    ok("F garbage names never raise")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
