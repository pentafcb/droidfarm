#!/usr/bin/env python3
"""Tests for state-load-robustness (manager only, no agent change).

GAP (one layer sideways from t152): t152 fixed the SHAPE of state files
(a valid JSON list/string/number/null coerced to {}), but the five
manager state loaders still caught only (FileNotFoundError,
json.JSONDecodeError). Two unreadable-file classes escaped that tuple
and killed the handler thread with no response on every caller:

  1. UnicodeDecodeError — a state file with invalid UTF-8 bytes (disk
     corruption, bad hand edit). json.load() raises it, and it is NOT a
     JSONDecodeError (both are ValueError subclasses, but neither is
     the other), so the old except missed it.
  2. OSError beyond FileNotFoundError — a state path that is a
     directory (IsADirectoryError), unreadable (PermissionError), or an
     I/O error. None of these is FileNotFoundError, so the old except
     missed them too.

Blast radius: load_providers / load_instances / load_accounts feed the
dashboard's /api/instances poll and the public /api/providers/register
route (which does providers.items()); load_tokens is called unguarded
by main(), so a bad-bytes tokens file killed the manager AT STARTUP
into a systemd restart loop — a total outage, where a missing tokens
file simply means "no sessions".

FIX (manager/app.py only): all five loaders now catch
(OSError, ValueError). That tuple covers the old classes
(FileNotFoundError is an OSError, JSONDecodeError is a ValueError) plus
both new ones (UnicodeDecodeError is a ValueError, IsADirectoryError /
PermissionError are OSErrors), so unreadable means "no state" exactly
like a missing file. Valid dict files load byte-identically; no
dashboard, agent, or wire-protocol change.

Covers:
  A. regression proof: bad-bytes json.load raises UnicodeDecodeError
     (not JSONDecodeError); directory open raises IsADirectoryError
     (not FileNotFoundError); subclass relations proving the new
     tuple covers old + new classes
  B-F. real loaders over bad-bytes file / directory path / valid dict /
     missing file / malformed JSON, for all five loaders
  G. load_tokens specifics: bad-bytes + directory -> {}, valid mixed
     entries still filter (future kept, expired + str-value dropped)
  H. source contracts + snapshot parity (agent untouched, still 1.0.45)
"""
import importlib.util
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
AGENT = os.path.join(REPO, "wing", "agent.py")
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


app = load("slr_app", APP)
agent = load("slr_agent", AGENT)
app_src = open(APP, encoding="utf-8").read()


def fn_src(name):
    start = app_src.index(f"def {name}(")
    try:
        end = app_src.index("\ndef ", start + 1)
    except ValueError:
        end = len(app_src)  # main() is the last function in the file
    return app_src[start:end]


TMP = tempfile.mkdtemp(prefix="slr-")
BAD_BYTES = os.path.join(TMP, "badbytes.json")
with open(BAD_BYTES, "wb") as f:
    f.write(b'{"a": "\xff\xfe"}')
DIR_PATH = os.path.join(TMP, "dir.json")
os.mkdir(DIR_PATH)

print("== A. regression proof ==")
try:
    with open(BAD_BYTES) as f:
        json.load(f)
    bad("bad-bytes json.load raises", "no exception")
except UnicodeDecodeError:
    ok("bad-bytes json.load raises UnicodeDecodeError")
try:
    with open(BAD_BYTES) as f:
        json.load(f)
    bad("UnicodeDecodeError is not JSONDecodeError", "no exception")
except json.JSONDecodeError:
    bad("UnicodeDecodeError is not JSONDecodeError", "was JSONDecodeError")
except UnicodeDecodeError:
    ok("UnicodeDecodeError is not JSONDecodeError")
try:
    open(DIR_PATH)
    bad("directory open raises", "no exception")
except IsADirectoryError:
    ok("directory open raises IsADirectoryError")
try:
    open(DIR_PATH)
    bad("IsADirectoryError is not FileNotFoundError", "no exception")
except FileNotFoundError:
    bad("IsADirectoryError is not FileNotFoundError", "was FileNotFoundError")
except IsADirectoryError:
    ok("IsADirectoryError is not FileNotFoundError")
check("new tuple covers old classes",
      issubclass(FileNotFoundError, OSError)
      and issubclass(json.JSONDecodeError, ValueError))
check("new tuple covers new classes",
      issubclass(UnicodeDecodeError, ValueError)
      and issubclass(IsADirectoryError, OSError)
      and issubclass(PermissionError, OSError))
main_src = fn_src("main")
check("main() calls load_tokens() unguarded",
      "    load_tokens()" in main_src)


def write(path, content):
    with open(path, "w") as f:
        f.write(content)


def loader_cases(label, const, fn):
    path = os.path.join(TMP, label + ".json")
    setattr(app, const, path)
    # unreadable shapes -> {} (the fix)
    setattr(app, const, BAD_BYTES)
    check(f"{label} bad-bytes file -> {{}}", fn() == {})
    setattr(app, const, DIR_PATH)
    check(f"{label} directory path -> {{}}", fn() == {})
    setattr(app, const, path)
    # unchanged behavior: missing / malformed / valid
    setattr(app, const, os.path.join(TMP, "missing-" + label + ".json"))
    check(f"{label} missing file -> {{}}", fn() == {})
    write(path, "{not json")
    setattr(app, const, path)
    check(f"{label} malformed JSON -> {{}}", fn() == {})
    valid = {"rbx1": {"memory_mb": 2048}}
    write(path, json.dumps(valid))
    check(f"{label} valid dict loads exactly", fn() == valid)


print("== B. load_providers ==")
loader_cases("providers", "PROVIDERS_FILE", app.load_providers)
print("== C. load_instances ==")
loader_cases("instances", "INSTANCES_FILE", app.load_instances)
print("== D. load_accounts ==")
loader_cases("accounts", "ACCOUNTS_FILE", app.load_accounts)
print("== E. load_login_results ==")
loader_cases("login_results", "LOGIN_RESULTS_FILE", app.load_login_results)

print("== F. load_tokens ==")
tok_path = os.path.join(TMP, "tokens.json")
app.TOKENS_FILE = BAD_BYTES
app.load_tokens()
check("tokens bad-bytes file -> {}", app._tokens == {})
app.TOKENS_FILE = DIR_PATH
app.load_tokens()
check("tokens directory path -> {}", app._tokens == {})
app.TOKENS_FILE = os.path.join(TMP, "missing-tokens.json")
app.load_tokens()
check("tokens missing file -> {}", app._tokens == {})
write(tok_path, "{not json")
app.TOKENS_FILE = tok_path
app.load_tokens()
check("tokens malformed JSON -> {}", app._tokens == {})
now = time.time()
write(tok_path, json.dumps({"good": now + 3600, "old": now - 10,
                            "bad": "soon"}))
app.load_tokens()
check("tokens future kept, expired + str-value dropped",
      app._tokens == {"good": now + 3600})

print("== G. source contracts + snapshot parity ==")
for fn_name in ("load_providers", "load_instances", "load_accounts",
                "load_login_results", "load_tokens"):
    check(f"{fn_name} catches (OSError, ValueError)",
          "except (OSError, ValueError):" in fn_src(fn_name))
check("no narrow state-loader except remains",
      "except (FileNotFoundError, json.JSONDecodeError):" not in app_src)
check("agent untouched, still 1.0.45", agent.VERSION == "1.0.45",
      repr(agent.VERSION))
check("snapshot app.py byte-identical to canonical",
      open(SNAP_APP, "rb").read() == open(APP, "rb").read())

print(f"PASS={PASS} FAIL={FAIL}")
