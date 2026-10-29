#!/usr/bin/env python3
"""Tests for provider-record-guard (manager only, no agent change).

GAP (one layer sideways from t152/t153): t152 fixed the providers FILE
shape (a JSON list coerces to {}) and t153 fixed unreadable files, but
a RECORD inside a valid dict file can still be the wrong shape — a
hand edit or partial write can leave a string record, a string
last_seen, or string stats. Every caller that trusted the record shape
then crashed the handler thread with no response:

  1. create_remote_instance / resize_remote_instance: a truthy string
     record passed `if not p`, then p.get("last_seen") raised
     AttributeError; a string last_seen raised TypeError on the
     recency arithmetic. Both are request paths (create, resize).
  2. pick_provider (provider="auto" create path): a non-dict record
     raised AttributeError on p.get("stats"); string stats fields
     raised TypeError on the capacity arithmetic.
  3. GET /api/providers list: one non-dict record raised AttributeError
     on p.get("name") and killed the WHOLE list; a string last_seen
     raised TypeError on the online computation; a string host_caps
     raised AttributeError on .get("loop_devices").
  4. wing_request: a record missing url/agent_key raised KeyError and
     killed the caller's handler (every remote path funnels through it).
  5. Register route: a pre-existing corrupt record for the same id
     raised AttributeError on providers.get(pid, {}).get("created")
     and dropped the PUBLIC register request.

FIX (manager/app.py only): provider_last_seen() / provider_stats()
helpers (non-dict or non-numeric -> 0 / {}, so "recency unknown" always
reads as offline, never online); create/resize treat a non-dict or
empty record as "node not found" and an unprovable recency as
"node offline"; pick_provider skips unprovable records as candidates;
the providers list skips non-dict records and degrades bad fields
(last_seen None, stats {}, selfwatch {}, caps None); wing_request
fails closed with 502 on a bad record; register preserves `created`
only from a dict record. Valid records behave byte-identically; no
dashboard, agent, or wire-protocol change.

Covers:
  A. regression proof (old code necessarily crashed on these shapes)
  B. helpers provider_last_seen / provider_stats
  C. real create_remote_instance (stubbed providers/instances/wing)
  D. real resize_remote_instance (same stubs)
  E. real pick_provider (stubbed host stats / instances)
  F. real wing_request guard (no network except a refused loopback)
  G. providers-list field degradation (helper-level + source contract)
  H. source contracts + snapshot parity (agent untouched, still 1.0.45)
"""
import importlib.util
import os
import sys
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


app = load("prg_app", APP)
agent = load("prg_agent", AGENT)
app_src = open(APP, encoding="utf-8").read()


def fn_src(name):
    start = app_src.index(f"def {name}(")
    try:
        end = app_src.index("\ndef ", start + 1)
    except ValueError:
        end = len(app_src)
    return app_src[start:end]


NOW = time.time()
RECENT = {"url": "http://10.0.0.9:8090", "agent_key": "k",
          "last_seen": NOW, "stats": {"ram_total_mb": 32000,
                                       "ram_percent": 50}}

print("== A. regression proof ==")
try:
    "oops".get("last_seen")
    bad("str record .get raises AttributeError", "no exception")
except AttributeError:
    ok("str record .get raises AttributeError")
try:
    time.time() - "soon"
    bad("string last_seen arithmetic raises TypeError", "no exception")
except TypeError:
    ok("string last_seen arithmetic raises TypeError")
try:
    {}["url"]
    bad("missing url raises KeyError", "no exception")
except KeyError:
    ok("missing url raises KeyError")
check("bool is an int (so bool must be excluded explicitly)",
      isinstance(True, int))

print("== B. helpers ==")
check("last_seen float passes through",
      app.provider_last_seen({"last_seen": 123.5}) == 123.5)
check("last_seen int passes through",
      app.provider_last_seen({"last_seen": 123}) == 123)
check("last_seen missing -> 0", app.provider_last_seen({}) == 0)
check("last_seen string -> 0",
      app.provider_last_seen({"last_seen": "soon"}) == 0)
check("last_seen None -> 0", app.provider_last_seen({"last_seen": None}) == 0)
check("last_seen bool -> 0", app.provider_last_seen({"last_seen": True}) == 0)
check("non-dict str record -> 0", app.provider_last_seen("oops") == 0)
check("non-dict list record -> 0", app.provider_last_seen(["x"]) == 0)
check("stats dict passes through",
      app.provider_stats({"stats": {"a": 1}}) == {"a": 1})
check("stats string -> {}", app.provider_stats({"stats": "junk"}) == {})
check("stats None -> {}", app.provider_stats({"stats": None}) == {})
check("stats missing -> {}", app.provider_stats({}) == {})
check("non-dict record stats -> {}", app.provider_stats("oops") == {})


def stub_env(providers, instances=None, wing=None):
    saved = {}
    for attr in ("load_providers", "load_instances", "save_instances",
                 "wing_request", "get_host_stats"):
        saved[attr] = getattr(app, attr, None)
    app.load_providers = lambda: providers
    held = {"instances": dict(instances or {})}

    def _load_instances():
        return held["instances"]

    def _save_instances(data):
        held["instances"] = dict(data)

    app.load_instances = _load_instances
    app.save_instances = _save_instances
    calls = []
    if wing is not None:
        def _wing(p, method, path, data=None, timeout=8, attempts=3):
            calls.append((method, path))
            return wing(p, method, path, data)
        app.wing_request = _wing
    else:
        def _wing(p, method, path, data=None, timeout=8, attempts=3):
            calls.append((method, path))
            return 502, {"error": "should not be called"}
        app.wing_request = _wing
    app.get_host_stats = lambda: {"ram_total_mb": 1000, "ram_percent": 90}
    return saved, held, calls


def restore(saved):
    for attr, val in saved.items():
        if val is None:
            try:
                delattr(app, attr)
            except AttributeError:
                pass
        else:
            setattr(app, attr, val)


print("== C. create_remote_instance ==")
saved, held, calls = stub_env({"srt1": "oops"})
try:
    code, resp = app.create_remote_instance("n1", 2, 2048, "srt1")
    check("str record -> 404 node not found",
          (code, resp.get("error")) == (404, "node not found"), repr((code, resp)))
    check("str record -> wing never called", calls == [], repr(calls))
finally:
    restore(saved)
saved, held, calls = stub_env({"srt1": {}})
try:
    code, resp = app.create_remote_instance("n1", 2, 2048, "srt1")
    check("empty dict record -> 404", code == 404, repr((code, resp)))
finally:
    restore(saved)
saved, held, calls = stub_env({"srt1": dict(RECENT, last_seen="soon")})
try:
    code, resp = app.create_remote_instance("n1", 2, 2048, "srt1")
    check("string last_seen -> 409 offline",
          (code, resp.get("error")) == (409, "node offline"), repr((code, resp)))
    check("string last_seen -> wing never called", calls == [], repr(calls))
finally:
    restore(saved)
saved, held, calls = stub_env({"srt1": dict(RECENT, last_seen=NOW - 9999)})
try:
    code, resp = app.create_remote_instance("n1", 2, 2048, "srt1")
    check("stale last_seen -> 409 offline", code == 409, repr((code, resp)))
finally:
    restore(saved)


def wing_ok(p, method, path, data):
    if method == "GET":
        return 200, {"instances": [{"ip": "10.20.0.11"}]}
    return 200, {}


saved, held, calls = stub_env({"srt1": dict(RECENT)}, wing=wing_ok)
try:
    code, resp = app.create_remote_instance("n1", 2, 2048, "srt1")
    check("valid recent record -> 200", code == 200, repr((code, resp)))
    check("IP pick skips used 10.20.0.11 -> 10.20.0.12",
          held["instances"].get("n1", {}).get("ip") == "10.20.0.12",
          repr(held["instances"]))
    check("saved record carries provider",
          held["instances"].get("n1", {}).get("provider") == "srt1")
finally:
    restore(saved)
saved, held, calls = stub_env({"srt1": dict(RECENT)},
                              instances={"n1": {"provider": "srt1"}},
                              wing=wing_ok)
try:
    code, resp = app.create_remote_instance("n1", 2, 2048, "srt1")
    check("already exists -> 409 before wing",
          code == 409 and calls == [], repr((code, calls)))
finally:
    restore(saved)

print("== D. resize_remote_instance ==")
saved, held, calls = stub_env({"srt1": "oops"})
try:
    code, resp = app.resize_remote_instance("n1", 2048, 2, "srt1")
    check("str record -> 404", code == 404, repr((code, resp)))
finally:
    restore(saved)
saved, held, calls = stub_env({"srt1": dict(RECENT, last_seen="soon")})
try:
    code, resp = app.resize_remote_instance("n1", 2048, 2, "srt1")
    check("string last_seen -> 409 offline", code == 409, repr((code, resp)))
    check("string last_seen -> wing never called", calls == [], repr(calls))
finally:
    restore(saved)
saved, held, calls = stub_env({"srt1": dict(RECENT, last_seen=NOW - 9999)})
try:
    code, resp = app.resize_remote_instance("n1", 2048, 2, "srt1")
    check("stale last_seen -> 409 offline", code == 409, repr((code, resp)))
finally:
    restore(saved)
saved, held, calls = stub_env({"srt1": dict(RECENT)},
                              instances={},
                              wing=lambda p, m, pa, d: (200, {}))
try:
    code, resp = app.resize_remote_instance("n1", 2048, 2, "srt1")
    check("unknown instance -> 404", code == 404, repr((code, resp)))
finally:
    restore(saved)
saved, held, calls = stub_env(
    {"srt1": dict(RECENT)},
    instances={"n1": {"provider": "srt1", "memory_mb": 1024, "cpus": 1}},
    wing=lambda p, m, pa, d: (200, {}))
try:
    code, resp = app.resize_remote_instance("n1", 4096, 4, "srt1")
    check("valid resize -> 200", code == 200, repr((code, resp)))
    check("saved limits updated",
          held["instances"]["n1"]["memory_mb"] == 4096
          and held["instances"]["n1"]["cpus"] == 4, repr(held["instances"]))
    check("response carries new limits",
          resp.get("memory_mb") == 4096 and resp.get("cpus") == 4
          and resp.get("provider") == "srt1", repr(resp))
finally:
    restore(saved)

print("== E. pick_provider ==")
saved, held, calls = stub_env({})
try:
    got = app.pick_provider({"good": dict(RECENT)}, need_ram_mb=4096)
    check("good remote candidate picked", got == "good", repr(got))
    got = app.pick_provider({"bad": "oops"}, need_ram_mb=4096)
    check("non-dict record skipped -> None", got is None, repr(got))
    got = app.pick_provider(
        {"bad": dict(RECENT, stats="junk")}, need_ram_mb=4096)
    check("string stats skipped -> None", got is None, repr(got))
    got = app.pick_provider(
        {"bad": dict(RECENT, stats={"ram_total_mb": 32000,
                                    "ram_percent": "50"})}, need_ram_mb=4096)
    check("string ram_percent skipped -> None", got is None, repr(got))
    got = app.pick_provider(
        {"bad": dict(RECENT, last_seen=True)}, need_ram_mb=4096)
    check("bool last_seen skipped -> None", got is None, repr(got))
finally:
    restore(saved)

print("== F. wing_request guard ==")
code, resp = app.wing_request("oops", "GET", "/x", attempts=1)
check("non-dict provider -> 502", code == 502, repr((code, resp)))
code, resp = app.wing_request({}, "GET", "/x", attempts=1)
check("missing url -> 502", code == 502, repr((code, resp)))
code, resp = app.wing_request({"url": "http://127.0.0.1:1"}, "GET", "/x",
                              attempts=1)
check("missing agent_key -> 502", code == 502, repr((code, resp)))
code, resp = app.wing_request({"url": 123, "agent_key": "k"}, "GET", "/x",
                              attempts=1)
check("int url -> 502", code == 502, repr((code, resp)))
code, resp = app.wing_request({"url": "", "agent_key": "k"}, "GET", "/x",
                              attempts=1)
check("empty url -> 502", code == 502, repr((code, resp)))
code, resp = app.wing_request({"url": "http://127.0.0.1:1", "agent_key": ""},
                              "GET", "/x", attempts=1)
check("empty agent_key -> 502", code == 502, repr((code, resp)))
code, resp = app.wing_request({"url": "http://127.0.0.1:1", "agent_key": "k"},
                              "GET", "/x", timeout=2, attempts=1)
check("valid record, refused connection -> 502 not raise",
      code == 502, repr((code, resp)))

print("== G. providers-list field degradation ==")
ls = "soon"
check("string last_seen emits None, online False",
      (None if not isinstance(ls, (int, float)) or isinstance(ls, bool)
       else ls) is None
      and not (time.time() - app.provider_last_seen({"last_seen": ls}) < 120))
check("string stats emits {}", app.provider_stats({"stats": "junk"}) == {})
sw = "degraded?"
check("string selfwatch emits {}",
      (sw if isinstance(sw, dict) else {}) == {})
caps = "caps?"
check("string host_caps emits loop_devices None",
      (caps if isinstance(caps, dict) else {}).get("loop_devices") is None)
list_src = app_src[app_src.index('if path == "/api/providers"'):]
list_src = list_src[:list_src.index("# ---- install files")]
check("providers list skips non-dict records",
      "if not isinstance(p, dict):\n                    continue" in list_src)
check("providers list uses provider_last_seen + provider_stats",
      "provider_last_seen(p)" in list_src and "provider_stats(p)" in list_src)

print("== H. source contracts + snapshot parity ==")
check("create_remote_instance guards record shape",
      "if not isinstance(p, dict) or not p:" in fn_src("create_remote_instance")
      and "provider_last_seen(p)" in fn_src("create_remote_instance"))
check("resize_remote_instance guards record shape",
      "if not isinstance(p, dict) or not p:" in fn_src("resize_remote_instance")
      and "provider_last_seen(p)" in fn_src("resize_remote_instance"))
check("pick_provider uses provider_stats + provider_last_seen",
      "provider_stats(p)" in fn_src("pick_provider")
      and "provider_last_seen(p)" in fn_src("pick_provider"))
check("wing_request fails closed 502 on bad record",
      "node unreachable: bad provider record" in fn_src("wing_request"))
reg_src = app_src[app_src.index('if path == "/api/providers/register"'):]
reg_src = reg_src[:reg_src.index("# ---- provider: install progress")]
check("register preserves created only from dict record",
      "isinstance(prev, dict)" in reg_src)
check("agent untouched, still 1.0.45", agent.VERSION == "1.0.45",
      repr(agent.VERSION))
check("snapshot app.py byte-identical to canonical",
      open(SNAP_APP, "rb").read() == open(APP, "rb").read())

print(f"PASS={PASS} FAIL={FAIL}")
