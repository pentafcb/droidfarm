#!/usr/bin/env python3
"""Tests for request-type-guard (manager only, no agent change).

GAP: t148/t149/t150 hardened numeric conversions and body shapes, but
the SAME dropped-connection class lurked in two values that are used
as an iterable / dict key without a type check:

  1. POST /api/instances/batch {names: 5} — batch_create_job did
     `[str(n) for n in names]` with no isinstance check, so a non-list
     `names` raised TypeError on iteration (int/bool) or silently
     iterated dict keys / string chars, killing the handler thread
     with no response (authenticated route, but one bad dashboard
     client or proxy-rewritten body was enough).
  2. POST /api/providers/heartbeat {provider_id: ["x"]} — the handler
     did `providers.get(pid)` before the agent-key check, so an
     unhashable provider_id raised TypeError and dropped the
     connection before auth ever ran. The same line also crashed on a
     corrupt providers file (a JSON list) and on a non-dict provider
     record (`p["agent_key"]` on a string), and an empty stored
     agent_key authenticated against an empty supplied key because
     hmac.compare_digest("", "") is True.

FIX (manager/app.py only): batch_create_job returns a clean 400
"names must be a list" for any truthy non-list `names` (falsy still
means "all instances"); the heartbeat handler coerces a non-dict
providers mapping to {}, a non-string provider_id to "", and fails
closed with 403 on a non-dict record, a missing key, or an empty
stored key. Valid inputs behave byte-identically (same 202/200
codes, same job/heartbeat shapes).

Covers:
  A. regression proof: iterating an int, dict-keying a list, and
     string-indexing with a string all raise TypeError (not a
     ValueError any handler catches); compare_digest("", "") is True
  B. real batch_create_job (stubbed I/O): non-list names -> 400,
     falsy/list names unchanged (202 totals, unknown-name 400)
  C. real do_POST heartbeat (stubbed I/O): unhashable/corrupt/
     missing-key/empty-key provider_id -> 403, valid -> 200
  D. source contracts + snapshot parity (agent untouched, still
     1.0.45, so no VERSION bump)
"""
import importlib.util
import io
import os
import sys

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


app = load("rtg_app", APP)
agent = load("rtg_agent", AGENT)
app_src = open(APP, encoding="utf-8").read()

print("== A. regression proof ==")
try:
    [str(n) for n in 5]
    bad("iterating int raises", "no exception")
except TypeError:
    ok("iterating int names raises TypeError")
try:
    {}.get(["x"])
    bad("unhashable dict key raises", "no exception")
except TypeError:
    ok("list provider_id as dict key raises TypeError")
import operator as _operator
try:
    _operator.getitem("junk", "agent_key")
    bad("string record indexing raises", "no exception")
except TypeError:
    ok("non-dict provider record indexing raises TypeError")
import hmac as _hmac
check("compare_digest('', '') is True (empty-key hole)",
      _hmac.compare_digest("", "") is True)

print("== B. batch_create_job ==")
orig_load, orig_sweep, orig_journal, orig_worker = (
    app.load_instances, app._sweep_batch_jobs,
    app._journal_batch_job, app._run_batch_job)
app.load_instances = lambda: {"rbx1": {}, "rbx2": {}}
app._sweep_batch_jobs = lambda: None
app._journal_batch_job = lambda job: None
app._run_batch_job = lambda jid: None
try:
    for label, names, want_code in [
            ("int names -> 400", 5, 400),
            ("dict names -> 400", {"rbx1": 1}, 400),
            ("string names -> 400", "rbx1", 400),
            ("bool names -> 400", True, 400),
            ("empty names -> 202 all", [], 202),
            ("None names -> 202 all", None, 202),
            ("list names -> 202", ["rbx1"], 202),
            ("unknown name -> 400", ["nope"], 400),
            ("int item coerced -> 400 unknown", [5], 400),
    ]:
        app._batch_jobs = {}
        try:
            code, resp = app.batch_create_job("stop", names)
        except Exception as e:  # noqa: BLE001 - crash IS the bug class
            bad(f"batch {label}", f"raised {type(e).__name__} (dropped call)")
            continue
        good = code == want_code
        if want_code == 202 and names in ([], None):
            good = good and resp.get("total") == 2
        if want_code == 202 and names == ["rbx1"]:
            good = good and resp.get("total") == 1
        if want_code == 400 and names in (5, {"rbx1": 1}, "rbx1", True):
            good = good and resp.get("error") == "names must be a list"
        check(f"batch {label}", good, repr((code, resp)))
    app._batch_jobs = {}
    code, resp = app.batch_create_job("stop", ["rbx1"])
    job = app._batch_jobs.get(resp.get("job_id", ""), {})
    check("batch valid job stored with list names",
          code == 202 and job.get("names") == ["rbx1"], repr(job))
finally:
    (app.load_instances, app._sweep_batch_jobs,
     app._journal_batch_job, app._run_batch_job) = (
        orig_load, orig_sweep, orig_journal, orig_worker)

print("== C. heartbeat do_POST ==")
saved = {}


def run_heartbeat(providers_obj, raw):
    h = object.__new__(app.Handler)
    h.path = "/api/providers/heartbeat"
    h.command = "POST"
    h.client_address = ("127.0.0.1", 1234)
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    out = {}

    def _json(code, payload):
        out["code"] = code
        out["payload"] = payload
        return None

    h._json = _json
    orig_lp, orig_sp = app.load_providers, app.save_providers
    app.load_providers = lambda: providers_obj
    app.save_providers = lambda p: saved.update({"providers": p})
    try:
        h.do_POST()
    finally:
        app.load_providers, app.save_providers = orig_lp, orig_sp
    return out


GOOD = {"p1": {"agent_key": "k1", "url": "http://10.0.0.1:8090"}}
import json as _json_mod


def body(pid, key="k1"):
    return _json_mod.dumps({"provider_id": pid, "agent_key": key}).encode()


for label, providers_obj, raw, want in [
        ("list provider_id -> 403", dict(GOOD), body(["x"]), 403),
        ("dict provider_id -> 403", dict(GOOD), body({"a": 1}), 403),
        ("int provider_id -> 403", dict(GOOD), body(5), 403),
        ("providers file is a list -> 403", ["p1"], body("p1"), 403),
        ("provider record is a string -> 403",
         {"p1": "junk"}, body("p1"), 403),
        ("record missing agent_key -> 403",
         {"p1": {"url": "http://x:8090"}}, body("p1"), 403),
        ("empty stored key + empty supplied -> 403",
         {"p1": {"agent_key": ""}}, body("p1", ""), 403),
        ("wrong key -> 403", dict(GOOD), body("p1", "nope"), 403),
        ("unknown provider -> 403", dict(GOOD), body("p9"), 403),
        ("valid -> 200", {"p1": {"agent_key": "k1",
                                 "url": "http://10.0.0.1:8090"}},
         body("p1"), 200),
        ("valid, minimal body -> 200",
         {"p1": {"agent_key": "k1"}}, body("p1"), 200),
]:
    saved.clear()
    try:
        out = run_heartbeat(providers_obj, raw)
    except Exception as e:  # noqa: BLE001 - crash IS the bug class
        bad(f"heartbeat {label}", f"raised {type(e).__name__} (dropped call)")
        continue
    check(f"heartbeat {label}", out.get("code") == want, repr(out))
saved.clear()
out = run_heartbeat({"p1": {"agent_key": "k1",
                            "url": "http://10.0.0.1:8090"}}, body("p1"))
check("heartbeat valid call saved providers with last_seen",
      out.get("code") == 200
      and isinstance(saved.get("providers", {}).get("p1"), dict)
      and "last_seen" in saved["providers"]["p1"], repr(out))

print("== D. source contracts + snapshots ==")
check("batch guard: non-list names -> 400",
      'if names and not isinstance(names, list):' in app_src)
check("heartbeat guard: non-string pid coerced",
      'if not isinstance(pid, str):' in app_src)
check("heartbeat guard: non-dict providers mapping",
      'if not isinstance(providers, dict):' in app_src)
check("heartbeat guard: non-dict record + empty stored key fail closed",
      'if (not isinstance(p, dict) or not p.get("agent_key")' in app_src)
check("agent untouched (still 1.0.45, no bump needed)",
      agent.VERSION == "1.0.45", repr(agent.VERSION))
check("snapshot app.py byte-identical to canonical",
      open(SNAP_APP, "rb").read() == open(APP, "rb").read())

print(f"PASS={PASS} FAIL={FAIL}")
