"""Unit tests for the remote-fetch exception-path cache fix (manager/app.py).

Gap: fetch_remote_statuses() serves the 30s last-known-good cache when a
wing node's /api/agent/instances fetch returns a non-200 response — but
NOT when wing_request raises (timeout, connection error, ...). The raise
path incremented the failure counter and continued without touching the
cache, so a single transient timeout (the most common failure mode for a
wedged or slow agent) flipped every card on that node to "stopped" at
once, while the node-fetch badge needs 6 consecutive failures (~60s)
before it says anything. The function's own docstring already promised
cache reuse "on a fetch failure" — the raise path violated it, and the
node-fetch-badge suite only asserted the counter on the raise path, never
the returned mapping, so the asymmetry shipped untested.

Fix: the except branch now serves the fresh last-known-good cache exactly
like the non-200 branch (same TTL check, same isinstance guard), then
continues. Counter behavior is unchanged: a raise still counts.

Run: python3 test_remote_fetch_exception_cache.py

Hermetic: the real app.py module is loaded; wing_request is faked per
case (scoped, restored afterwards). No docker, no network, no real files.
"""
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
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


def check(name, cond, detail=""):
    if cond:
        ok(name)
    else:
        bad(name, detail)


def load_app():
    spec = importlib.util.spec_from_file_location("rfec_app", APP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rfec_app"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    try:
        app = load_app()
    except Exception as e:  # noqa: BLE001 - report, don't crash the suite
        bad("app module loads", repr(e))
        print(f"PASS={PASS} FAIL={FAIL}")
        return 1

    TTL = app._REMOTE_STATUS_TTL
    modes = {"srt": "ok", "vul": "ok"}
    calls = {"n": 0}

    def fake_wing(p, _method, _path, **_kw):
        calls["n"] += 1
        kind = modes.get((p or {}).get("tag"), "ok")
        if kind == "raise":
            raise ConnectionError("boom")
        if kind == "raise-value":
            raise ValueError("boom")
        if kind == "fail":
            return 500, {}
        if kind == "ok2":
            return 200, {"instances": [{"name": "vul1", "running": True}]}
        return 200, {"instances": [{"name": "srt1", "running": True}]}

    def prov(tag, fresh=True):
        return {tag: {"last_seen": time.time() if fresh else time.time() - 121,
                      "tag": tag}}

    orig = app.wing_request
    app.wing_request = fake_wing
    try:
        # ---------- A. raise path serves the cache (the fix) ----------
        app._remote_fetch_failures.clear()
        app._remote_status_cache.clear()
        modes["srt"] = "ok"
        out = app.fetch_remote_statuses(prov("srt"))
        check("success caches srt1", ("srt", "srt1") in out)
        check("success leaves counter at 0",
              app.remote_fetch_failures("srt") == 0)
        modes["srt"] = "raise"
        out = app.fetch_remote_statuses(prov("srt"))
        check("raise counts as a failure",
              app.remote_fetch_failures("srt") == 1)
        check("raise after success still serves cached srt1 (THE FIX)",
              ("srt", "srt1") in out)
        out = app.fetch_remote_statuses(prov("srt"))
        check("second raise counts again",
              app.remote_fetch_failures("srt") == 2)
        check("second raise still serves cache",
              ("srt", "srt1") in out)
        modes["srt"] = "fail"
        out = app.fetch_remote_statuses(prov("srt"))
        check("non-200 still counts", app.remote_fetch_failures("srt") == 3)
        check("non-200 still serves cache (unchanged)",
              ("srt", "srt1") in out)
        modes["srt"] = "ok2"
        out = app.fetch_remote_statuses(prov("srt"))
        check("success resets counter", app.remote_fetch_failures("srt") == 0)
        check("success serves fresh instance", ("srt", "vul1") in out)
        check("success replaces the cache (old entry gone)",
              ("srt", "srt1") not in out)

        # ---------- B. cache boundaries on the raise path ----------
        app._remote_fetch_failures.clear()
        modes["srt"] = "raise"
        fresh_per = {("srt", "srt1"): {"name": "srt1", "running": True}}
        app._remote_status_cache["srt"] = (time.monotonic() - (TTL - 1),
                                           fresh_per)
        out = app.fetch_remote_statuses(prov("srt"))
        check("raise with fresh-aged cache serves it",
              ("srt", "srt1") in out)
        app._remote_status_cache["srt"] = (time.monotonic() - (TTL + 1),
                                           fresh_per)
        out = app.fetch_remote_statuses(prov("srt"))
        check("raise with stale cache serves nothing",
              ("srt", "srt1") not in out)
        app._remote_status_cache.clear()
        before = app.remote_fetch_failures("srt")
        out = app.fetch_remote_statuses(prov("srt"))
        check("raise with no cache serves nothing", out == {})
        check("raise with no cache still counts",
              app.remote_fetch_failures("srt") == before + 1)
        app._remote_status_cache["srt"] = (time.monotonic(), ["not", "a", "dict"])
        out = app.fetch_remote_statuses(prov("srt"))
        check("raise with non-dict cache serves nothing", out == {})

        # ---------- C. skips, multi-provider, never-raises ----------
        app._remote_fetch_failures.clear()
        app._remote_status_cache.clear()
        modes["srt"] = "raise"
        before = calls["n"]
        out = app.fetch_remote_statuses(prov("srt", fresh=False))
        check("stale-heartbeat provider never attempted on raise mode",
              calls["n"] == before)
        check("stale-heartbeat provider contributes nothing", out == {})
        check("stale-heartbeat provider not counted",
              app.remote_fetch_failures("srt") == 0)
        out = app.fetch_remote_statuses({"srt": "junk"})
        check("non-dict provider entry skipped, not counted",
              app.remote_fetch_failures("srt") == 0)
        app._remote_fetch_failures.clear()
        app._remote_status_cache.clear()
        modes["srt"] = "ok"
        app.fetch_remote_statuses(prov("srt"))
        modes["srt"] = "raise"
        modes["vul"] = "ok"
        both = dict(prov("srt"))
        both.update(prov("vul"))
        out = app.fetch_remote_statuses(both)
        check("two providers: raising one serves cache, ok one serves fresh",
              ("srt", "srt1") in out and ("vul", "srt1") in out)
        modes["srt"] = "raise-value"
        app._remote_status_cache.clear()
        app.fetch_remote_statuses(prov("srt"))  # ok first would be needed;
        # reset to a cached state via a success, then raise a ValueError
        modes["srt"] = "ok"
        app.fetch_remote_statuses(prov("srt"))
        modes["srt"] = "raise-value"
        out = app.fetch_remote_statuses(prov("srt"))
        check("non-ConnectionError raise also serves cache",
              ("srt", "srt1") in out)
        check("providers None -> {} (never raises)",
              app.fetch_remote_statuses(None) == {})
        check("providers non-dict -> {} (never raises)",
              app.fetch_remote_statuses(["srt"]) == {})

        # ---------- D. source contract ----------
        with open(APP, "r") as f:
            src = f.read()
        start = src.index("def fetch_remote_statuses")
        end = src.index("def patch_remote_status", start)
        fn = src[start:end]
        check("raise branch serves cache in source (3 cache serves total)",
              fn.count("out.update(per)") == 3)
        check("raise-branch comment names the cache fix",
              "still served" in fn)
        check("docstring still promises cache reuse on failure",
              "last-known snapshot is reused" in fn)
    finally:
        app.wing_request = orig

    print(f"PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
