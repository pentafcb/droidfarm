#!/usr/bin/env python3
"""Tests for state-shape-guard (manager only, no agent change).

GAP: the manager's JSON state loaders returned json.load() verbatim, so
a state file holding a valid JSON value of the wrong shape crashed
every caller instead of behaving like a missing file:

  1. load_providers / load_instances / load_accounts on a file holding
     a list, string, number, or null returned that value; every caller
     doing .items()/.get() then raised AttributeError, the handler
     thread died, and the connection dropped with NO response. Only
     load_login_results already guarded (isinstance dict else {}), and
     only a few call sites guarded — the other ~70 call sites (including
     the dashboard's /api/instances poll and the public
     /api/providers/register route, which does providers.items()) did not.
  2. load_tokens did json.load(f).items() inline: a list file raised
     AttributeError, and a dict with a non-numeric expiry raised
     TypeError on `e > time.time()`. main() calls load_tokens()
     unguarded, so either shape killed the manager AT STARTUP into a
     systemd restart loop — a total dashboard outage, where a missing
     tokens file simply means "no sessions".

FIX (manager/app.py only): load_providers / load_instances /
load_accounts coerce any non-dict parsed value to {} (the
load_login_results idiom — non-dict means "no state", exactly like a
missing file); load_tokens coerces a non-dict file to {} and drops
malformed entries individually (valid sessions survive a single bad
expiry). Valid dict files load byte-identically; no dashboard, agent,
or wire-protocol change.

Covers:
  A. regression proof: list .items() -> AttributeError, tokens
     comprehension on a list -> AttributeError, on a str expiry ->
     TypeError, main() calls load_tokens() unguarded
  B/C/D. real load_providers / load_instances / load_accounts:
     list/str/number/null -> {}, valid dict -> exact
  E. missing file -> {}, malformed JSON -> {} (unchanged behavior)
  F. real load_tokens: list/str/null -> {}, future kept, expired
     dropped, str-value entry dropped while a good entry survives
  G. source contracts + snapshot parity (agent untouched, still 1.0.45)
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


app = load("ssg_app", APP)
agent = load("ssg_agent", AGENT)
app_src = open(APP, encoding="utf-8").read()


def fn_src(name):
    start = app_src.index(f"def {name}(")
    try:
        end = app_src.index("\ndef ", start + 1)
    except ValueError:
        end = len(app_src)  # main() is the last function in the file
    return app_src[start:end]


print("== A. regression proof ==")
try:
    json.loads("[1, 2]").items()
    bad("list state .items() raises", "no exception")
except AttributeError:
    ok("list state .items() raises AttributeError")
try:
    {t: e for t, e in json.loads("[1]").items() if e > time.time()}
    bad("tokens comprehension on list raises", "no exception")
except AttributeError:
    ok("tokens comprehension on list raises AttributeError")
try:
    {t: e for t, e in {"tok": "soon"}.items() if e > time.time()}
    bad("tokens comprehension on str expiry raises", "no exception")
except TypeError:
    ok("tokens comprehension on str expiry raises TypeError")
main_src = fn_src("main")
check("main() calls load_tokens() unguarded",
      "    load_tokens()" in main_src)

TMP = tempfile.mkdtemp(prefix="ssg-")


def write(path, content):
    with open(path, "w") as f:
        f.write(content)


def loader_cases(label, const, fn):
    path = os.path.join(TMP, label + ".json")
    setattr(app, const, path)
    for shape, raw in [("list", "[1, 2]"), ("string", '"junk"'),
                       ("number", "42"), ("null", "null")]:
        write(path, raw)
        check(f"{label} {shape} file -> {{}}", fn() == {})
    valid = {"rbx1": {"memory_mb": 2048}}
    write(path, json.dumps(valid))
    check(f"{label} valid dict loads exactly", fn() == valid)


print("== B. load_providers ==")
loader_cases("providers", "PROVIDERS_FILE", app.load_providers)
print("== C. load_instances ==")
loader_cases("instances", "INSTANCES_FILE", app.load_instances)
print("== D. load_accounts ==")
loader_cases("accounts", "ACCOUNTS_FILE", app.load_accounts)

print("== E. missing / malformed (unchanged behavior) ==")
missing = os.path.join(TMP, "missing.json")
app.PROVIDERS_FILE = missing
check("providers missing file -> {}", app.load_providers() == {})
bad_json = os.path.join(TMP, "bad.json")
write(bad_json, "{not json")
app.INSTANCES_FILE = bad_json
check("instances malformed JSON -> {}", app.load_instances() == {})
app.ACCOUNTS_FILE = missing
check("accounts missing file -> {}", app.load_accounts() == {})

print("== F. load_tokens ==")
tok_path = os.path.join(TMP, "tokens.json")
app.TOKENS_FILE = tok_path
for shape, raw in [("list", "[1]"), ("string", '"junk"'), ("null", "null")]:
    write(tok_path, raw)
    app.load_tokens()
    check(f"tokens {shape} file -> {{}}", app._tokens == {})
now = time.time()
write(tok_path, json.dumps({"good": now + 3600, "old": now - 10,
                            "bad": "soon"}))
app.load_tokens()
check("tokens future expiry kept, expired + str-value dropped",
      app._tokens == {"good": now + 3600})

print("== G. source contracts + snapshot parity ==")
check("load_providers coerces non-dict",
      "isinstance(d, dict)" in fn_src("load_providers"))
check("load_instances coerces non-dict",
      "isinstance(d, dict)" in fn_src("load_instances"))
check("load_accounts coerces non-dict",
      "isinstance(d, dict)" in fn_src("load_accounts"))
tok_src = fn_src("load_tokens")
check("load_tokens coerces non-dict + filters bad expiries",
      "isinstance(raw, dict)" in tok_src
      and "isinstance(e, (int, float))" in tok_src)
check("load_login_results guard unchanged",
      "isinstance(d, dict)" in fn_src("load_login_results"))
check("agent untouched, still 1.0.45", agent.VERSION == "1.0.45",
      repr(agent.VERSION))
check("snapshot app.py byte-identical to canonical",
      open(SNAP_APP, "rb").read() == open(APP, "rb").read())

print(f"PASS={PASS} FAIL={FAIL}")
