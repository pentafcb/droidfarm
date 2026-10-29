"""Unit tests for atomic token-file writes (manager/app.py).

Run: python3 test_token_atomic_write.py

REAL BUGS FIXED:
  get_static_token() wrote STATIC_TOKEN_FILE with a bare open("w") + write:
  a crash (kill -9, ENOSPC) between the truncate and the write left a
  truncated token file behind. The next boot read the short token, failed
  the len>=32 check, silently REGENERATED the "static" join token with no
  log line - and every saved node-enrollment one-liner started 403ing with
  no visible cause (same mystery-403 class as the 2026-10-06 Windows
  one-liner incident). Fix: tmp+rename atomic write + a loud WARNING when
  regenerating over an existing-but-invalid file.
  save_tokens() had the identical torn-write class on TOKENS_FILE: a crash
  mid-write left truncated JSON; the next boot's load_tokens() hit
  JSONDecodeError and logged EVERY dashboard session out. Fix: tmp+rename.

Covers:
  get_static_token: fresh generation (format, persistence, 600 perms),
    valid-existing passthrough (byte-identical, mtime untouched),
    invalid-existing (short/torn) -> regenerates + warns loudly,
    unreadable (is-a-directory) -> valid token, never raises, warns,
    unpersistable dir -> valid token, never raises, warns,
    behavioral atomicity proof (the final path is NEVER opened for
    writing - only the .tmp path is),
    source contract (no bare open(STATIC_TOKEN_FILE,"w") remains).
  save_tokens: round-trip through load_tokens, corrupt tokens.json ->
    load gives {} (behavior preserved), unwritable dir -> never raises,
    behavioral atomicity proof, source contract.

Hermetic: app.py loaded via importlib; STATIC_TOKEN_FILE / TOKENS_FILE
monkeypatched to tmp paths; builtins.open wrapped (not replaced) for the
atomicity proofs; no network, no docker, no real DATA_DIR touched.
"""
import builtins
import contextlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

app = load("tokenatomic_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
with open(os.path.join(HERE, "..", "..", "manager", "app.py")) as f:
    APP_SRC = f.read()

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")

HEX64 = re.compile(r"^[0-9a-f]{64}$")

def fresh_env():
    d = tempfile.mkdtemp(prefix="tokatom_")
    app.STATIC_TOKEN_FILE = os.path.join(d, "static_join_token")
    app.TOKENS_FILE = os.path.join(d, "tokens.json")
    return d

def capture_stdout(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **k)
    return r, buf.getvalue()

print("== get_static_token: fresh generation ==")
d = fresh_env()
t = app.get_static_token()
check("fresh token is 64 hex chars", bool(HEX64.match(t or "")))
check("file persisted with identical content",
      open(app.STATIC_TOKEN_FILE).read().strip() == t)
mode = stat.S_IMODE(os.stat(app.STATIC_TOKEN_FILE).st_mode)
check("token file mode is 600", mode == 0o600)
t2 = app.get_static_token()
check("second call returns the SAME token (stable)", t2 == t)

print("== get_static_token: valid existing passthrough ==")
d = fresh_env()
with open(app.STATIC_TOKEN_FILE, "w") as f:
    f.write("ab" * 32)
before = os.stat(app.STATIC_TOKEN_FILE)
t = app.get_static_token()
check("existing valid token returned unchanged", t == "ab" * 32)
after = os.stat(app.STATIC_TOKEN_FILE)
check("valid file untouched (mtime identical)",
      (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size))

print("== get_static_token: torn/invalid existing -> regenerate + warn ==")
d = fresh_env()
with open(app.STATIC_TOKEN_FILE, "w") as f:
    f.write("short")  # what a torn write (old code) leaves behind
t, out = capture_stdout(app.get_static_token)
check("torn token regenerated to valid 64-hex", bool(HEX64.match(t or "")))
check("persisted file now holds the new token",
      open(app.STATIC_TOKEN_FILE).read().strip() == t)
check("loud WARNING printed on regeneration",
      "WARNING" in out and "regenerat" in out)

print("== get_static_token: unreadable path (is a directory) ==")
d = fresh_env()
os.mkdir(app.STATIC_TOKEN_FILE)
t, out = capture_stdout(app.get_static_token)
check("returns a valid token, never raises", bool(HEX64.match(t or "")))
check("warns loudly", "WARNING" in out)

print("== get_static_token: unpersistable dir ==")
d = fresh_env()
app.STATIC_TOKEN_FILE = os.path.join(d, "no-such-dir", "static_join_token")
t, out = capture_stdout(app.get_static_token)
check("returns a valid token, never raises", bool(HEX64.match(t or "")))
check("warns about in-memory token", "in-memory" in out)

print("== get_static_token: behavioral atomicity proof ==")
d = fresh_env()
writes_to_final = []
real_open = builtins.open
def spy_open(path, mode="r", *a, **k):
    if isinstance(path, str) and os.path.abspath(path) == os.path.abspath(app.STATIC_TOKEN_FILE) \
            and isinstance(mode, str) and ("w" in mode or "a" in mode or "+" in mode):
        writes_to_final.append((path, mode))
    return real_open(path, mode, *a, **k)
builtins.open = spy_open
try:
    t = app.get_static_token()          # fresh generate
    t = app.get_static_token()          # passthrough read
    with real_open(app.STATIC_TOKEN_FILE, "w") as f:
        f.write("short")               # simulate torn write
    t = app.get_static_token()          # regenerate path
finally:
    builtins.open = real_open
check("valid token produced across all paths", bool(HEX64.match(t or "")))
check("final path NEVER opened for writing (tmp+rename only)",
      writes_to_final == [])

print("== save_tokens / load_tokens ==")
d = fresh_env()
app._tokens = {"tokA": 9999999999.0, "tokB": 9999999999.0}
app.save_tokens()
check("tokens.json is valid JSON",
      json.load(open(app.TOKENS_FILE)) == app._tokens)
app._tokens = {}
app.load_tokens()
check("load_tokens round-trips", app._tokens == {"tokA": 9999999999.0, "tokB": 9999999999.0})
with open(app.TOKENS_FILE, "w") as f:
    f.write("{truncated")  # torn write, old failure mode
app._tokens = {"x": 1}
app.load_tokens()
check("corrupt tokens.json -> {} (logout-all behavior preserved, no crash)",
      app._tokens == {})

print("== save_tokens: never raises, atomicity proof ==")
d = fresh_env()
app.TOKENS_FILE = os.path.join(d, "no-such-dir", "tokens.json")
app._tokens = {"tokA": 9999999999.0}
try:
    app.save_tokens()
    raised = False
except Exception:
    raised = True
check("unwritable dir -> save_tokens never raises", not raised)
d = fresh_env()
writes_to_final = []
builtins.open = spy_open
try:
    app._tokens = {"tokA": 9999999999.0}
    app.save_tokens()
finally:
    builtins.open = real_open
check("final tokens path NEVER opened for writing", writes_to_final == [])
check("tokens.json holds the saved tokens",
      json.load(open(app.TOKENS_FILE)) == {"tokA": 9999999999.0})

print("== source contract: no bare non-atomic writes remain ==")
check("no open(STATIC_TOKEN_FILE, \"w\") left",
      'open(STATIC_TOKEN_FILE, "w")' not in APP_SRC)
check("no open(TOKENS_FILE, \"w\") left",
      'open(TOKENS_FILE, "w")' not in APP_SRC)
check("get_static_token uses tmp+rename",
      'STATIC_TOKEN_FILE + ".tmp"' in APP_SRC and
      "os.replace(tmp, STATIC_TOKEN_FILE)" in APP_SRC)
check("save_tokens uses tmp+rename",
      'TOKENS_FILE + ".tmp"' in APP_SRC and
      "os.replace(tmp, TOKENS_FILE)" in APP_SRC)

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
