#!/usr/bin/env python3
"""Tests for non-dict-json-guard (manager + agent _read_json, agent 1.0.45).

GAP: t148/t149 hardened numeric conversions, but the SAME dropped-
connection class lurked one layer up: json.loads accepts any JSON
value, and both servers' _read_json returned the parsed value verbatim.
Every POST caller then does data.get(...), so a body of `["x"]`,
`"username"`, `123`, `true`, or `null` raised AttributeError, the
handler thread died (handle_error only logs), and the connection
dropped with NO response. Worst case: /api/login is unauthenticated,
so anyone could drop handler threads with a one-line body; the
register/install-progress routes died BEFORE their join-token check
(the .get runs first). The roblox-account route had its own variant:
a string body containing "username" passes the `in` checks and then
crashes on body["username"] (TypeError); a list body silently no-ops
with a misleading 200.

FIX: _read_json (manager + agent) coerces any non-dict parsed value
to {} — the single choke point covering all 12 manager and 3 agent
callers, so each gets its normal clean 400/401 from validation instead
of a dropped connection. The roblox-account route additionally rejects
a non-dict body with a clean 400 "invalid json" (the resize route
already coerces via validate_resize_params). Wing agent 1.0.44 ->
1.0.45; VERSION tuples widened, exact pins moved.

Covers:
  A. versions: agent 1.0.45 + snapshot parity + manager gate compat
  B. regression proof: list .get raises AttributeError (not a
     ValueError any handler catches); string-body indexing raises
     TypeError after passing the `in` check
  C. manager _read_json matrix: non-dict bodies -> {}, dict bodies
     pass through intact, malformed/garbage/over-cap -> {}
  D. agent _read_json matrix: same coercion on the wing side
  E. roblox-account route (real do_POST, stubbed I/O): non-dict
     bodies -> 400, valid dict body -> 200 with the account saved
  F. source contracts: isinstance coercion in both _read_json
     implementations + the roblox-account guard
"""
import importlib.util
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
AGENT = os.path.join(REPO, "wing", "agent.py")
APP = os.path.join(REPO, "manager", "app.py")
SNAP_AGENT = os.path.join(REPO, "pending-deploy", "remote-autologin", "agent.py")
SNAP_APP = os.path.join(REPO, "pending-deploy", "remote-autologin", "app.py")

PASS = FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok: {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {name} {detail}")


def check(name, cond, detail=""):
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


agent = load("ndg_agent", AGENT)
app = load("ndg_app", APP)
ag_src = open(AGENT, encoding="utf-8").read()
app_src = open(APP, encoding="utf-8").read()


def fake_reader(mod, body, length=None):
    """A Handler instance with stubbed headers/rfile, for _read_json."""
    h = object.__new__(mod.Handler)
    n = len(body) if length is None else length
    h.headers = {"Content-Length": str(n)}
    h.rfile = io.BytesIO(body)
    return h


print("== A. versions + snapshots ==")
check("agent VERSION is 1.0.45", agent.VERSION == "1.0.45", repr(agent.VERSION))
check("snapshot agent.py byte-identical to canonical",
      open(SNAP_AGENT, "rb").read() == open(AGENT, "rb").read())
check("snapshot app.py byte-identical to canonical",
      open(SNAP_APP, "rb").read() == open(APP, "rb").read())
check("manager version gate accepts 1.0.45",
      app._version_at_least("1.0.45", "1.0.2"))

print("== B. regression proof ==")
try:
    ["x"].get("password", "")
    bad("list .get raises", "no exception")
except AttributeError:
    ok("list body .get raises AttributeError")
except Exception as e:  # noqa: BLE001 - proving the class
    bad("list body .get raises AttributeError", f"got {type(e).__name__}")
check("AttributeError is not a ValueError",
      not issubclass(AttributeError, ValueError))
body_str = "username"
check("string body passes the roblox-account `in` check",
      "username" in body_str)
try:
    body_str["username"]
    bad("string body indexing raises", "no exception")
except TypeError:
    ok("string body indexing raises TypeError after passing `in`")

print("== C. manager _read_json matrix ==")
for label, raw, want in [
        ("list body -> {}", b'["x"]', {}),
        ("string body -> {}", b'"username"', {}),
        ("number body -> {}", b"123", {}),
        ("true body -> {}", b"true", {}),
        ("null body -> {}", b"null", {}),
        ("dict body passes through", b'{"password": "pw"}',
         {"password": "pw"}),
        ("nested dict passes through", b'{"a": {"b": [1, 2]}}',
         {"a": {"b": [1, 2]}}),
        ("empty body -> {}", b"", {}),
        ("malformed body -> {}", b"{nope", {}),
        ("garbage Content-Length -> {}", b'{"a": 1}', {}),
        ("over-cap Content-Length -> {}", b'{"a": 1}', {}),
        ("negative Content-Length -> {}", b'{"a": 1}', {}),
]:
    if label.startswith("garbage"):
        h = fake_reader(app, raw, length="junk")
    elif label.startswith("over-cap"):
        h = fake_reader(app, raw, length=2097153)
    elif label.startswith("negative"):
        h = fake_reader(app, raw, length=-5)
    else:
        h = fake_reader(app, raw)
    got = h._read_json()
    check(f"manager {label}", got == want, repr(got))

print("== D. agent _read_json matrix ==")
for label, raw, want in [
        ("list body -> {}", b'["x"]', {}),
        ("string body -> {}", b'"tap"', {}),
        ("number body -> {}", b"42", {}),
        ("null body -> {}", b"null", {}),
        ("dict body passes through", b'{"type": "tap", "x": 500}',
         {"type": "tap", "x": 500}),
        ("empty body -> {}", b"", {}),
        ("malformed body -> {}", b"[broken", {}),
        ("garbage Content-Length -> {}", b'{"a": 1}', {}),
        ("over-cap Content-Length -> {}", b'{"a": 1}', {}),
]:
    if label.startswith("garbage"):
        h = fake_reader(agent, raw, length="junk")
    elif label.startswith("over-cap"):
        h = fake_reader(agent, raw, length=2097153)
    else:
        h = fake_reader(agent, raw)
    got = h._read_json()
    check(f"agent {label}", got == want, repr(got))

print("== E. roblox-account route (real do_POST) ==")
saved = {}


def run_route(raw):
    h = object.__new__(app.Handler)
    h.path = "/api/instances/rbx1/roblox-account"
    h.command = "POST"
    h.client_address = ("127.0.0.1", 1234)
    h._read_body = lambda *a, **k: raw
    h.headers = {}
    h._authed = lambda: True  # do_POST's global gate; route logic is under test
    out = {}

    def _json(code, payload):
        out["code"] = code
        out["payload"] = payload
        return None

    h._json = _json
    orig_load, orig_save = app.load_accounts, app.save_accounts
    app.load_accounts = lambda: dict(saved)
    app.save_accounts = lambda a: saved.update(a)
    try:
        h.do_POST()
    finally:
        app.load_accounts, app.save_accounts = orig_load, orig_save
    return out


for label, raw, want_code in [
        ("string body -> 400", b'"username"', 400),
        ("list body -> 400", b'["username"]', 400),
        ("number body -> 400", b"7", 400),
        ("null body -> 400", b"null", 400),
        ("valid dict body -> 200", b'{"username": "u1"}', 200),
]:
    saved.clear()
    out = run_route(raw)
    check(f"roblox-account {label}",
          out.get("code") == want_code, repr(out))
check("roblox-account valid dict saved the username",
      saved.get("rbx1", {}).get("username") == "u1", repr(saved))

print("== F. source contracts ==")
check("manager _read_json coerces non-dict",
      "parsed if isinstance(parsed, dict) else {}" in app_src)
check("agent _read_json coerces non-dict",
      "parsed if isinstance(parsed, dict) else {}" in ag_src)
check("roblox-account route guards non-dict body",
      "if not isinstance(body, dict):" in app_src)
check("no bare json.loads return left in manager _read_json",
      "return json.loads(self.rfile.read(ln)" not in app_src)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
