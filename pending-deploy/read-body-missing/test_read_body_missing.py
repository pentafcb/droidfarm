#!/usr/bin/env python3
"""Unit tests for read-body-missing: Handler._read_body never existed.

POST /api/instances/<name>/resize and POST /api/instances/<name>/roblox-account
both called self._read_body() since the first commit, but no such method was
ever defined on Handler (or inherited): both routes raised AttributeError and
the connection dropped with no response, so dashboard Resize and the
per-instance Account Save never worked. Bulk import uses a different endpoint,
which is why nobody noticed. The fix adds _read_body (raw bytes, same 2MB cap
as _read_json) next to _read_json; the two call sites are unchanged, so invalid
JSON still surfaces as their 400.

Hermetic: real manager/app.py import (temp JERUSALEM_DATA + test password set
before import, so no real state is touched), fake rfile/headers for the unit
section, a real BoundedHTTPServer on 127.0.0.1 for the live section, and a
git-HEAD AST check as the pre-fix regression proof. No docker, no VPS.
"""
import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
import urllib.error

BASE = tempfile.mkdtemp(prefix="read-body-test-")
os.environ["JERUSALEM_DATA"] = BASE
os.environ["JERUSALEM_PASSWORD"] = "read-body-test-pw"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "manager"))
import app  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} {detail}")


# ---------- A. method exists, call sites intact ------------------------------
check("A1 Handler defines _read_body", hasattr(app.Handler, "_read_body"))
SRC = open(os.path.join(os.path.dirname(__file__), "..", "..", "manager", "app.py")).read()
check("A2 resize route still calls _read_body",
      "self._read_body()" in SRC)
check("A3 roblox-account route still calls _read_body",
      SRC.count("self._read_body()") == 2)


def _fake_handler(body, length=None):
    h = app.Handler.__new__(app.Handler)
    h.headers = {"Content-Length": str(len(body)) if length is None else length}
    h.rfile = io.BytesIO(body)
    return h


# ---------- B. unit behavior -------------------------------------------------
h = _fake_handler(b'{"a": 1}')
check("B1 exact bytes returned", app.Handler._read_body(h) == b'{"a": 1}')
h = _fake_handler(b"", length="0")
check("B2 zero length -> b''", app.Handler._read_body(h) == b"")
h = _fake_handler(b"", length=None)
h.headers = {}
check("B3 missing Content-Length -> b''", app.Handler._read_body(h) == b"")


class _ExplodingReader(io.BytesIO):
    def __init__(self):
        super().__init__(b"x" * 10)
        self.read_calls = 0

    def read(self, n=-1):
        self.read_calls += 1
        raise AssertionError("must not read an over-cap body")


h = _fake_handler(b"", length="999999999")
h.rfile = _ExplodingReader()
check("B4 over-cap -> b'' without reading",
      app.Handler._read_body(h) == b"" and h.rfile.read_calls == 0)
h = _fake_handler(b"abc", length="-5")
check("B5 negative length -> b''", app.Handler._read_body(h) == b"")
h = _fake_handler(b"abc", length="not-a-number")
check("B6 garbage length -> b'' never raises",
      app.Handler._read_body(h) == b"")
h = _fake_handler(b"abc", length="2097152")
check("B7 at-cap length reads (not rejected)",
      app.Handler._read_body(h) == b"abc")
check("B8 _read_body sits next to _read_json in Handler",
      SRC.index("def _read_body") > SRC.index("def _read_json"))


# ---------- C. pre-fix regression proof (original commit) --------------------
# Pinned to 2ed1b84 (the first DroidFarm source commit), NOT HEAD: HEAD will
# contain the fix after this package is committed, which would flip C1.
head_src = subprocess.run(
    ["git", "-C", os.path.join(os.path.dirname(__file__), "..", ".."),
     "show", "2ed1b84:manager/app.py"],
    capture_output=True, text=True, check=True).stdout
head_tree = ast.parse(head_src)
head_handler = next(n for n in ast.walk(head_tree)
                    if isinstance(n, ast.ClassDef) and n.name == "Handler")
head_methods = {n.name for n in head_handler.body
                if isinstance(n, ast.FunctionDef)}
check("C1 original commit Handler lacks _read_body (the bug)",
      "_read_body" not in head_methods)
check("C2 original commit already had a call site (roblox-account; "
      "resize joined it in 663c8ab) — broken since day one",
      head_src.count("self._read_body()") == 1)


# ---------- D. live server: routes answer instead of dropping ----------------
app.Handler.log_message = lambda *a: None
srv = app.BoundedHTTPServer(("127.0.0.1", 0), app.Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def _post(path, body, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=body, method="POST")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:  # connection drop = the pre-fix symptom
        return None, {"dropped": str(e)}


status, login = _post("/api/login", json.dumps(
    {"password": "read-body-test-pw"}).encode())
check("D1 login works", status == 200 and "token" in login, repr(login))
tok = login.get("token", "")

status, resp = _post("/api/instances/rbx1/roblox-account",
                     json.dumps({"username": "acct-user",
                                 "password": "dummy-pw-for-test",
                                 "auto_login": False}).encode(), tok)
check("D2 account save returns 200 (was a dropped connection)",
      status == 200 and resp.get("ok") is True, repr(resp))
acct_file = os.path.join(BASE, "roblox_accounts.json")
saved = json.load(open(acct_file)) if os.path.exists(acct_file) else {}
check("D3 account persisted with the username",
      saved.get("rbx1", {}).get("username") == "acct-user")

status, resp = _post("/api/instances/rbx1/roblox-account",
                     b"{not json", tok)
check("D4 invalid JSON still 400 (not a silent ok)",
      status == 400, repr(resp))
status, resp = _post("/api/instances/rbx1/resize",
                     json.dumps({}).encode(), tok)
check("D5 resize with empty params answers 400 (was dropped)",
      status == 400, repr(resp))
status, resp = _post("/api/instances/rbx1/roblox-account",
                     json.dumps({"username": "x"}).encode())
check("D6 unauthenticated save still 401",
      status == 401, repr(resp))
status, resp = _post("/api/instances/rbx1/roblox-account",
                     json.dumps({"clear": True}).encode(), tok)
check("D7 account clear returns 200",
      status == 200 and resp.get("ok") is True, repr(resp))

srv.shutdown()
srv.server_close()
import shutil
shutil.rmtree(BASE, ignore_errors=True)
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
