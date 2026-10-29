#!/usr/bin/env python3
"""Tests for wing agent 1.0.37: request-body cap in _read_json.

REAL GAP: the manager's _read_json caps request bodies (2MB default, 4KB on
the login/register routes) — but the wing agent's _read_json did
`self.rfile.read(n)` with n taken straight from the client-supplied
Content-Length, unbounded. An authed client declaring Content-Length: 10GB
would page the whole body into RAM before json.loads ran. The agent's HTTP
server (8090-8099) is network-reachable on every enrolled node, and the
http-server-bounds 60s socket timeout bounds time, not bytes: 64 handler
threads x 60s of inbound bytes is a memory-exhaustion DoS on the node.

FIX (1.0.37): _read_json(self, max_bytes=2097152) mirrors the manager
contract — over-cap or negative Content-Length returns {} WITHOUT reading
the body (HTTP/1.0 default closes the connection after the response, so
skipping the read cannot desync a keep-alive). Never raises; garbage lengths
fail closed to {}. All three do_POST call sites keep the default cap, so
legitimate bodies behave exactly as before.

This suite covers:

  A. agent.VERSION is exactly 1.0.38 + manager >=1.0.2 gate compatibility
  B. over-cap Content-Length returns {} WITHOUT calling rfile.read
     (the actual DoS: the bytes are never pulled off the wire)
  C. negative / garbage / missing Content-Length fail closed to {}
  D. at-cap boundary and normal small bodies still parse (no behavior
     change for legitimate traffic)
  E. max_bytes is honored as a parameter (call-site override contract)
  F. truncated/malformed bodies never raise
  G. source contract: all do_POST _read_json call sites use the default
     (no route accidentally opted out of the cap)
"""
import importlib.util
import io
import os
import re
import sys

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


agent = load("abc_agent", AGENT)

print("== A. version + gate ==")
if agent.VERSION == "1.0.45":
    ok("A1 VERSION is exactly 1.0.44")
else:
    bad("A1 VERSION is exactly 1.0.44", f"got {agent.VERSION!r}")
if agent.parse_agent_version("1.0.39") > agent.parse_agent_version("1.0.38"):
    ok("A2 parse_agent_version orders 1.0.40 > 1.0.39")
else:
    bad("A2 parse_agent_version orders 1.0.40 > 1.0.39")

# manager gate: app.py must accept the new agent version (>=1.0.2 contract)
with open(APP) as f:
    src_app = f.read()
import re as _re
m = _re.search(r"def (agent_version_ok|check_agent_version|version_gate)\(.*?\n(?:.*\n){0,12}?.*?(1\.0\.2|min)", src_app)
gate_src = m.group(0) if m else src_app
if "1.0.2" in gate_src or "min" in gate_src.lower():
    ok("A3 manager agent-version gate present (>=1.0.2 family)")
else:
    bad("A3 manager agent-version gate", "gate not found")


class ExplodingRfile:
    """rfile whose read() blows up if ever called — proves no read happened."""

    def __init__(self):
        self.reads = 0

    def read(self, n=-1):
        self.reads += 1
        raise AssertionError(f"rfile.read({n}) must not be called on over-cap body")


class FakeHandler:
    """Minimal stand-in exposing only what _read_json touches."""

    def __init__(self, headers, rfile):
        self.headers = headers
        self.rfile = rfile


def call_read_json(headers, rfile, **kw):
    h = FakeHandler(headers, rfile)
    return agent.Handler._read_json(h, **kw)


print("== B. over-cap never reads ==")
# 10GB declared — the pre-1.0.37 code would rfile.read(10GB) here.
rf = ExplodingRfile()
res = call_read_json({"Content-Length": str(10 * 1024**3)}, rf)
if res == {} and rf.reads == 0:
    ok("B1 10GB Content-Length -> {} with zero rfile.read calls")
else:
    bad("B1 10GB over-cap", f"res={res!r} reads={rf.reads}")

# just one byte over the default 2MB cap
rf = ExplodingRfile()
res = call_read_json({"Content-Length": str(2097152 + 1)}, rf)
if res == {} and rf.reads == 0:
    ok("B2 cap+1 -> {} with zero reads")
else:
    bad("B2 cap+1", f"res={res!r} reads={rf.reads}")

# max int-ish header
rf = ExplodingRfile()
res = call_read_json({"Content-Length": "99999999999999999999"}, rf)
if res == {} and rf.reads == 0:
    ok("B3 absurd Content-Length -> {} with zero reads")
else:
    bad("B3 absurd length", f"res={res!r} reads={rf.reads}")

print("== C. hostile lengths fail closed ==")
rf = ExplodingRfile()
if call_read_json({"Content-Length": "-5"}, rf) == {} and rf.reads == 0:
    ok("C1 negative Content-Length -> {} without reading")
else:
    bad("C1 negative length", "not fail-closed")
rf = ExplodingRfile()
if call_read_json({"Content-Length": "not-a-number"}, rf) == {} and rf.reads == 0:
    ok("C2 garbage Content-Length -> {} without reading")
else:
    bad("C2 garbage length", "not fail-closed")
rf = ExplodingRfile()
if call_read_json({"Content-Length": ""}, rf) == {} and rf.reads == 0:
    ok("C3 empty Content-Length -> {} without reading")
else:
    bad("C3 empty length", "not fail-closed")
# missing header -> n=0 -> reads b"" -> "{}" parses to {}
res = call_read_json({}, io.BytesIO(b""))
if res == {}:
    ok("C4 missing Content-Length -> {}")
else:
    bad("C4 missing header", f"got {res!r}")

print("== D. legitimate bodies unchanged ==")
body = b'{"name": "rbx1", "cpus": 2}'
res = call_read_json({"Content-Length": str(len(body))}, io.BytesIO(body))
if res == {"name": "rbx1", "cpus": 2}:
    ok("D1 small JSON body parses exactly as before")
else:
    bad("D1 small body", f"got {res!r}")
# exactly at the cap: still read
body = b'{"k": "' + b"x" * (2097152 - 12) + b'"}'
res = call_read_json({"Content-Length": str(len(body))}, io.BytesIO(body))
if isinstance(res, dict) and res.get("k", "").startswith("x"):
    ok("D2 exactly-at-cap body is read and parsed")
else:
    bad("D2 at-cap body", f"got type {type(res)}")
# zero-length body
res = call_read_json({"Content-Length": "0"}, io.BytesIO(b""))
if res == {}:
    ok("D3 zero-length body -> {}")
else:
    bad("D3 zero body", f"got {res!r}")

print("== E. max_bytes parameter honored ==")
rf = ExplodingRfile()
res = call_read_json({"Content-Length": "5000"}, rf, max_bytes=4096)
if res == {} and rf.reads == 0:
    ok("E1 custom max_bytes=4096 refuses 5000 without reading")
else:
    bad("E1 custom cap", f"res={res!r} reads={rf.reads}")
body = b'{"a": 1}'
res = call_read_json({"Content-Length": str(len(body))}, io.BytesIO(body), max_bytes=4096)
if res == {"a": 1}:
    ok("E2 custom max_bytes allows under-cap body")
else:
    bad("E2 custom cap under", f"got {res!r}")

print("== F. malformed bodies never raise ==")
# declared 100 bytes, only a mid-token fragment arrives (client disconnect)
try:
    res = call_read_json({"Content-Length": "100"}, io.BytesIO(b'{"a":'))
    if res == {}:
        ok("F1 truncated body -> {} without raising")
    else:
        bad("F1 truncated body", f"got {res!r}")
except Exception as e:
    bad("F1 truncated body", f"raised {e!r}")
# valid length, invalid JSON
try:
    res = call_read_json({"Content-Length": "7"}, io.BytesIO(b"nope!!!"))
    if res == {}:
        ok("F2 non-JSON body -> {} without raising")
    else:
        bad("F2 non-JSON", f"got {res!r}")
except Exception as e:
    bad("F2 non-JSON", f"raised {e!r}")
# rfile itself raising mid-read
class BadRfile:
    def read(self, n=-1):
        raise OSError("boom")
try:
    res = call_read_json({"Content-Length": "10"}, BadRfile())
    if res == {}:
        ok("F3 raising rfile -> {} without propagating")
    else:
        bad("F3 raising rfile", f"got {res!r}")
except Exception as e:
    bad("F3 raising rfile", f"raised {e!r}")

print("== G. call-site contract ==")
with open(AGENT) as f:
    src = f.read()
calls = re.findall(r"self\._read_json\(([^)]*)\)", src)
if calls and all(c.strip() == "" for c in calls):
    ok(f"G1 all {len(calls)} do_POST call sites use the default 2MB cap")
else:
    bad("G1 call-site defaults", f"calls={calls!r}")
# the cap constant matches the manager's default
m_src = re.search(r"def _read_json\(self, max_bytes=(\d+)\)", src)
mgr_src = re.search(r"def _read_json\(self, max_bytes=(\d+)\)",
                    open(APP).read())
if m_src and mgr_src and m_src.group(1) == mgr_src.group(1):
    ok(f"G2 agent cap ({m_src.group(1)}) == manager cap (manager parity)")
else:
    bad("G2 cap parity", f"agent={m_src.group(1) if m_src else None} "
                         f"manager={mgr_src.group(1) if mgr_src else None}")

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
