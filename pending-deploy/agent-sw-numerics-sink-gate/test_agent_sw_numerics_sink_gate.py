#!/usr/bin/env python3
"""Tests for the agent sw_* numerics sink gate (agent-sw-numerics-sink-gate).

Census finding (2026-10-28): the t173 session closed the sw_* loop-driving
counts (binder pool) and named the remaining same-class items as milder:
`sw_probe_agent_http()` coerced its port with int() but never range-checked
it, and `sw_trim_container_logs()` relied on exception handling for bad
trim_max/trim_keep. Auditing the trim sink found the "mild" label was
wrong for one case: with a negative trim_keep the old code did
seek(-trim_keep, SEEK_END) = seek(+N, SEEK_END) — past EOF — so read()
returned b"" and the trim rewrote the docker json log to ZERO bytes.
A negative trim_max made every log "over threshold" on every pass, and
a trim_keep larger than the file made seek() land before the file start
and WARN on every pass forever.

The fix (wing/agent.py only, no manager/dashboard/wire change, no
VERSION bump — the manager's agent gate is >= 1.0.2, and bumping would
churn 53 test files' VERSION assertions for zero behavioral gain):
shared-coercion helpers _sw_coerce_probe_port [1, 65535],
_sw_coerce_probe_timeout (positive finite), _sw_coerce_trim_bytes
(positive int, no upper bound — docker json logs legitimately run to
tens of GB). Invalid values fail closed before any touch: probe ->
("bad", False) with zero urlopen calls; trim -> return with one WARN
per day (event-window deduped, never silent, never spammy). In the loop,
trim_keep >= file size is now a silent no-op skip (keeping the whole
file is not a trim). Valid paths are byte-identical (default 1GB/1GB,
trim_max/trim_keep overrides, probe 200/500/truncated/empty).

Covers:
  A. regression proof (old-logic simulation: negative keep wipes a log;
     old int() accepted 0/True/99999 as ports)
  B. _sw_coerce_probe_port matrix
  C. probe hostile ports -> ("bad", False), zero urlopen calls
  D. probe valid paths via stubbed urlopen (ok/bad/truncated/empty,
     string/float/boundary ports, URL carries the coerced port)
  E. probe timeout gate (hostile -> bad + zero calls; valid -> ok)
  F. _sw_coerce_trim_bytes matrix
  G. trim hostile params -> file untouched, one WARN per day, event file
  H. trim valid paths unchanged (trim to keep, tail bytes, same inode,
     under-threshold untouched, logged, event noted, overrides)
  I. trim_keep >= size -> silent no-op skip (no WARN loop)
  J. gate placement + sibling sink gates kept + deploy wiring
  K. negative proof (int(99999) succeeds but the gate refuses it;
     gated trim leaves a file the old logic would have wiped)
"""
import importlib.util
import inspect
import os
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
DEPLOY_ALL = os.path.join(REPO, "pending-deploy", "apply-all", "deploy-all.sh")

PASS = FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok: {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {name} {detail}")


def check(cond, name, detail=""):
    if cond:
        ok(name)
    else:
        bad(name, detail)


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


agent = load("aswng_agent", AGENT)

NAN = float("nan")
INF = float("inf")


def read_log(st):
    p = os.path.join(st, "selfwatch.log")
    try:
        with open(p) as fh:
            return fh.read()
    except Exception:
        return ""


# --- A. regression proof ------------------------------------------------------
# Old trim logic, simulated verbatim on a scratch file: seek(-trim_keep)
# with trim_keep=-500 lands 500 bytes PAST EOF; read() -> b""; the file
# is rewritten to zero bytes.
tmp = tempfile.mkdtemp()
oldf = os.path.join(tmp, "old-json.log")
open(oldf, "wb").write(b"z" * 2000)
with open(oldf, "r+b") as fh:
    fh.seek(-(-500), os.SEEK_END)
    tail = fh.read()
    fh.seek(0)
    fh.write(tail)
    fh.truncate()
check(os.path.getsize(oldf) == 0,
      "A1 old trim logic: negative keep rewrites a 2000-byte log to 0 bytes")
check(int(0) == 0 and int(True) == 1 and int(99999) == 99999,
      "A2 old probe logic: int() accepts 0/True/99999 as ports")

# --- B. _sw_coerce_probe_port matrix ------------------------------------------
for val, want in [(1, 1), (65535, 65535), (8090, 8090), ("8090", 8090),
                  (" 8090 ", 8090), (8090.0, 8090)]:
    check(agent._sw_coerce_probe_port(val) == want,
          f"B valid port {val!r} -> {want}")
for val in [0, -1, 65536, 99999, True, False, None, "bogus", "", "80.5",
            80.5, NAN, INF, [], {}]:
    check(agent._sw_coerce_probe_port(val) is None,
          f"B hostile port {val!r} -> None")

# --- C/D/E. probe via stubbed urlopen -----------------------------------------


class FakeResp:
    def __init__(self, code, body=None, read_raises=False):
        self._code = code
        self._body = body
        self._read_raises = read_raises

    def getcode(self):
        return self._code

    def read(self):
        if self._read_raises:
            raise IOError("truncated")
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class UrlopenStub:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append((req.full_url, timeout))
        return self.resp


orig_urlopen = urllib.request.urlopen
try:
    for val in [0, 65536, True, "bogus", None, 80.5, NAN, -1, "", 99999]:
        stub = UrlopenStub(FakeResp(200, b"{}"))
        urllib.request.urlopen = stub
        r = agent.sw_probe_agent_http(val)
        check(r == ("bad", False) and stub.calls == [],
              f"C hostile port {val!r} -> bad, zero urlopen calls",
              repr((r, stub.calls)))

    def probe_with(resp, port=8090, timeout=None):
        stub = UrlopenStub(resp)
        urllib.request.urlopen = stub
        kw = {} if timeout is None else {"timeout": timeout}
        r = agent.sw_probe_agent_http(port, **kw)
        return r, stub

    r, stub = probe_with(FakeResp(200, b'{"ok": true}'))
    check(r == ("ok", False), "D1 healthy 200+body -> ok", repr(r))
    r, _ = probe_with(FakeResp(500, b"error"))
    check(r == ("bad", False), "D2 500 -> bad, not truncated", repr(r))
    r, _ = probe_with(FakeResp(200, b"x", read_raises=True))
    check(r == ("bad", True), "D3 read raises -> bad+truncated", repr(r))
    r, _ = probe_with(FakeResp(200, b""))
    check(r == ("bad", True), "D4 empty 200 -> bad+truncated", repr(r))
    r, stub = probe_with(FakeResp(200, b"{}"), port="8090")
    check(r == ("ok", False) and stub.calls[0][0] == "http://127.0.0.1:8090/api/agent/health",
          "D5 string port coerced into URL", repr(stub.calls))
    r, stub = probe_with(FakeResp(200, b"{}"), port=8090.0)
    check(r == ("ok", False) and ":8090/" in stub.calls[0][0],
          "D6 integral float port coerced into URL", repr(stub.calls))
    r, stub = probe_with(FakeResp(200, b"{}"), port=1)
    check(r == ("ok", False) and ":1/" in stub.calls[0][0],
          "D7 boundary port 1 probes", repr(stub.calls))
    r, stub = probe_with(FakeResp(200, b"{}"), port=65535)
    check(r == ("ok", False) and ":65535/" in stub.calls[0][0],
          "D8 boundary port 65535 probes", repr(stub.calls))

    for val in [0, -1, NAN, INF, True, "10", None]:
        stub = UrlopenStub(FakeResp(200, b"{}"))
        urllib.request.urlopen = stub
        r = agent.sw_probe_agent_http(8090, timeout=val)
        check(r == ("bad", False) and stub.calls == [],
              f"E hostile timeout {val!r} -> bad, zero urlopen calls",
              repr((r, stub.calls)))
    r, stub = probe_with(FakeResp(200, b"{}"), timeout=10)
    check(r == ("ok", False) and stub.calls[0][1] == 10.0,
          "E valid timeout 10 passes through as float", repr(stub.calls))
    r, _ = probe_with(FakeResp(200, b"{}"), timeout=2.5)
    check(r == ("ok", False), "E valid timeout 2.5 -> ok", repr(r))
finally:
    urllib.request.urlopen = orig_urlopen

# --- F. _sw_coerce_trim_bytes matrix ------------------------------------------
for val, want in [(1, 1), (1000, 1000), (1073741824, 1073741824),
                  ("500", 500), (500.0, 500)]:
    check(agent._sw_coerce_trim_bytes(val) == want,
          f"F valid trim bytes {val!r} -> {want}")
for val in [0, -1, True, None, "bogus", "", 500.5, NAN, INF, []]:
    check(agent._sw_coerce_trim_bytes(val) is None,
          f"F hostile trim bytes {val!r} -> None")

# --- G/H/I. trim against real temp files --------------------------------------


def trim_setup(size, data=None):
    st = tempfile.mkdtemp()
    ld = tempfile.mkdtemp()
    c1 = os.path.join(ld, "contX")
    os.makedirs(c1)
    tgt = os.path.join(c1, "x-json.log")
    if data is None:
        data = bytes(range(256)) * (size // 256 + 1)
        data = data[:size]
    open(tgt, "wb").write(data)
    glob_pat = os.path.join(ld, "*", "*-json.log")
    return st, tgt, glob_pat, data


for tmax, tkeep, label in [(-100, 500, "negative trim_max"),
                           (1000, -500, "negative trim_keep"),
                           (0, 500, "zero trim_max"),
                           (1000, 0, "zero trim_keep"),
                           ("bogus", 500, "junk trim_max"),
                           (1000, 500.5, "fractional trim_keep")]:
    st, tgt, pat, _ = trim_setup(2000)
    agent.sw_trim_container_logs(st_dir=st, log_glob=pat,
                                 trim_max=tmax, trim_keep=tkeep)
    check(os.path.getsize(tgt) == 2000 and "log-trim: trimmed" not in read_log(st),
          f"G hostile {label} -> file untouched, no trim", str(os.path.getsize(tgt)))

st, tgt, pat, _ = trim_setup(2000)
agent.sw_trim_container_logs(st_dir=st, log_glob=pat, trim_max=-100, trim_keep=500)
agent.sw_trim_container_logs(st_dir=st, log_glob=pat, trim_max=-100, trim_keep=500)
check(read_log(st).count("WARN log-trim: invalid") == 1,
      "G badconfig WARN exactly once across two calls",
      repr(read_log(st).count("WARN log-trim: invalid")))
check(os.path.isfile(os.path.join(st, "log.trim.badconfig.ev")),
      "G badconfig event file noted")

st, tgt, pat, data = trim_setup(3072)
ino_before = os.stat(tgt).st_ino
small = os.path.join(os.path.dirname(tgt), "small-json.log")
open(small, "wb").write(b"s" * 500)
agent.sw_trim_container_logs(st_dir=st, log_glob=pat, trim_max=1000, trim_keep=500)
check(os.path.getsize(tgt) == 500, "H over-threshold trimmed to KEEP",
      str(os.path.getsize(tgt)))
check(open(tgt, "rb").read() == data[-500:], "H kept bytes are the tail")
check(os.stat(tgt).st_ino == ino_before, "H same inode (fd-safe)")
check(os.path.getsize(small) == 500, "H under-threshold file untouched")
check("log-trim: trimmed" in read_log(st), "H trim logged")
check(os.path.isfile(os.path.join(st, "log.trim.ev")), "H log.trim event noted")
st2, tgt2, pat2, _ = trim_setup(1500)
agent.sw_trim_container_logs(st_dir=st2, log_glob=pat2, trim_max=1000, trim_keep=100)
check(os.path.getsize(tgt2) == 100, "H trim_keep override: trimmed to 100",
      str(os.path.getsize(tgt2)))

st, tgt, pat, _ = trim_setup(2000)
agent.sw_trim_container_logs(st_dir=st, log_glob=pat, trim_max=1000, trim_keep=5000)
check(os.path.getsize(tgt) == 2000 and "WARN log-trim: failed" not in read_log(st),
      "I keep > size -> untouched, no WARN loop")
st, tgt, pat, _ = trim_setup(2000)
agent.sw_trim_container_logs(st_dir=st, log_glob=pat, trim_max=1000, trim_keep=2000)
check(os.path.getsize(tgt) == 2000,
      "I keep == size -> untouched (identity trim skipped)")

# --- J. gate placement + sibling gates kept + deploy wiring -------------------
src_probe = inspect.getsource(agent.sw_probe_agent_http)
check("_sw_coerce_probe_port(port)" in src_probe,
      "J placement: probe gates port")
check(src_probe.index("_sw_coerce_probe_port") < src_probe.index("urllib.request.urlopen"),
      "J placement: probe gate precedes urlopen")
src_trim = inspect.getsource(agent.sw_trim_container_logs)
check("_sw_coerce_trim_bytes(trim_max)" in src_trim,
      "J placement: trim gates trim_max")
check(src_trim.index("_sw_coerce_trim_bytes") < src_trim.index("glob.glob"),
      "J placement: trim gate precedes glob")
check("trim_keep >= size" in src_trim,
      "J placement: keep>=size no-op skip present")
src = open(AGENT, encoding="utf-8").read()
for marker, label in [
        ("agent-binder-count-sink-gate", "sibling kept: binder count gate"),
        ("agent-create-numerics-sink-gate", "sibling kept: create numerics gate"),
        ("agent-resize-numerics-sink-gate", "sibling kept: resize numerics gate"),
        ("agent-status-delete-sink-gate", "sibling kept: status/delete name gates")]:
    check(marker in src, label)
check(agent.VERSION == "1.0.45",
      "J VERSION deliberately unchanged at 1.0.45 (no bump needed)",
      repr(agent.VERSION))
deploy = open(DEPLOY_ALL, encoding="utf-8").read()
check("agent-sw-numerics-sink-gate" in deploy,
      "J deploy wiring: t174 preflight line present in deploy-all.sh")
check("wing/agent.py" in deploy,
      "J deploy wiring: agent.py already in apply-all stage list")

# --- K. negative proof ---------------------------------------------------------
check(int(99999) == 99999 and agent._sw_coerce_probe_port(99999) is None,
      "K negative proof: int(99999) succeeds but the gate refuses it")
st, tgt, pat, _ = trim_setup(2000)
agent.sw_trim_container_logs(st_dir=st, log_glob=pat, trim_max=1000, trim_keep=-500)
check(os.path.getsize(tgt) == 2000 and os.path.getsize(oldf) == 0,
      "K negative proof: gated trim leaves a file the old logic wiped")

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
