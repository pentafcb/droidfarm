#!/usr/bin/env python3
"""Tests for fleet-wide resource totals (local + wing nodes).

GAP: fleet_resource_summary() only summed the local docker-stats cache
while the dashboard fleet bar is labelled "RAM de flota" — with wing nodes
in the fleet, usage was silently undercounted, `instances_seen` counted
only local instances, and the denominator (host_mem_mb) excluded wing-node
RAM entirely. manager/app.py now takes the handler's remote entries
(already cleaned per-card) plus fresh wing-node RAM, keeping the exact
same 4-key shape so the dashboard label renders unchanged, now truthfully.

Covers (hermetic):
  A. fleet_resource_summary() baseline (no args) — identical to old behavior
  B. remote_instances — sums, counting, garbage tolerance
  C. remote_host_ram_mb param — denominator, failure interaction
  D. remote_host_ram_mb(providers) — staleness rule, stats shape
  E. /api/instances wiring (source-level)
  F. dashboard contract — updateResCov unchanged
  G. _fleet_number validator
"""
import importlib.util
import math
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
HTML = os.path.join(REPO, "manager", "static", "index.html")

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
    spec = importlib.util.spec_from_file_location("df_app", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def set_cache(app, entries):
    now = app.time.time()
    with app._resource_lock:
        app._resource_cache.clear()
        for name, mem, cpu, age in entries:
            app._resource_cache[name] = {"mem_used_mb": mem, "cpu_pct": cpu,
                                         "ts": now - age}


def main():
    print("fleet-resources-remote tests")
    try:
        app = load_app()
    except Exception as e:  # noqa: BLE001
        bad("import manager/app.py", str(e))
        print(f"\nPASS={PASS} FAIL={FAIL}")
        sys.exit(1)

    orig_host = app.host_mem_total_mb
    app.host_mem_total_mb = lambda: 31744
    try:
        # ---------- A. baseline (no args) = old behavior ----------
        print("== A. baseline ==")
        set_cache(app, [("a", 400.0, 10.0, 0), ("b", 800.5, 20.25, 0),
                        ("stale", 9999.0, 99.0, app._RESOURCE_TTL + 5)])
        s = app.fleet_resource_summary()
        check("baseline sums mem (stale excluded)", s["mem_used_mb"] == 1200.5)
        check("baseline sums cpu", abs(s["cpu_pct"] - 30.2) < 0.05)
        check("baseline instances_seen fresh only", s["instances_seen"] == 2)
        check("baseline host_mem_mb", s["host_mem_mb"] == 31744)
        check("baseline shape keys",
              set(s) == {"mem_used_mb", "cpu_pct", "instances_seen",
                         "host_mem_mb"})
        app.host_mem_total_mb = lambda: (_ for _ in ()).throw(OSError("x"))
        try:
            s2 = app.fleet_resource_summary()
            check("baseline host-read failure -> None", s2["host_mem_mb"] is None)
        finally:
            app.host_mem_total_mb = lambda: 31744

        # ---------- B. remote_instances ----------
        print("== B. remote_instances ==")
        set_cache(app, [("a", 400.0, 10.0, 0)])
        s = app.fleet_resource_summary(
            remote_instances=[{"mem_used_mb": 500.0, "cpu_pct": 15.0},
                              {"mem_used_mb": 250.5, "cpu_pct": 5.5}])
        check("remote mem summed", s["mem_used_mb"] == 1150.5)
        check("remote cpu summed", abs(s["cpu_pct"] - 30.5) < 0.05)
        check("remote instances_seen added", s["instances_seen"] == 3)
        s = app.fleet_resource_summary(
            remote_instances=[{"mem_used_mb": None, "cpu_pct": 99.0}])
        check("mem-None entry not counted", s["instances_seen"] == 1)
        check("mem-None entry cpu ignored", s["cpu_pct"] == 10.0)
        s = app.fleet_resource_summary(
            remote_instances=[{"mem_used_mb": 100.0, "cpu_pct": None}])
        check("mem-ok cpu-None counted", s["instances_seen"] == 2)
        check("mem-ok cpu-None cpu untouched", s["cpu_pct"] == 10.0)
        for label, bad_mem in [("string", "abc"), ("NaN", float("nan")),
                               ("inf", float("inf")), ("negative", -5.0),
                               ("over-cap", 1000001.0)]:
            s = app.fleet_resource_summary(
                remote_instances=[{"mem_used_mb": bad_mem, "cpu_pct": 50.0}])
            check(f"garbage mem {label} skipped",
                  s["instances_seen"] == 1 and s["mem_used_mb"] == 400.0
                  and s["cpu_pct"] == 10.0)
        s = app.fleet_resource_summary(
            remote_instances=[{"mem_used_mb": 100.0, "cpu_pct": "junk"}])
        check("garbage cpu skipped, mem counted",
              s["instances_seen"] == 2 and s["mem_used_mb"] == 500.0
              and s["cpu_pct"] == 10.0)
        s = app.fleet_resource_summary(remote_instances=["nope", 42, None])
        check("non-dict entries skipped",
              s["instances_seen"] == 1 and s["mem_used_mb"] == 400.0)
        s = app.fleet_resource_summary(remote_instances=[])
        check("empty remote list = baseline",
              s["mem_used_mb"] == 400.0 and s["instances_seen"] == 1)
        s = app.fleet_resource_summary(remote_instances=None)
        check("None remote = baseline",
              s["mem_used_mb"] == 400.0 and s["instances_seen"] == 1)

        # ---------- C. remote_host_ram_mb param ----------
        print("== C. remote_host_ram_mb param ==")
        set_cache(app, [("a", 400.0, 10.0, 0)])
        s = app.fleet_resource_summary(remote_host_ram_mb=8192)
        check("remote RAM added to denominator", s["host_mem_mb"] == 39936)
        s = app.fleet_resource_summary(remote_host_ram_mb=0)
        check("remote RAM 0 = baseline", s["host_mem_mb"] == 31744)
        s = app.fleet_resource_summary(remote_host_ram_mb=None)
        check("remote RAM None = baseline", s["host_mem_mb"] == 31744)
        s = app.fleet_resource_summary(remote_host_ram_mb=-100)
        check("remote RAM negative ignored", s["host_mem_mb"] == 31744)
        s = app.fleet_resource_summary(remote_host_ram_mb="junk")
        check("remote RAM garbage ignored", s["host_mem_mb"] == 31744)
        app.host_mem_total_mb = lambda: (_ for _ in ()).throw(OSError("x"))
        try:
            s = app.fleet_resource_summary(remote_host_ram_mb=8192)
            check("host-read failure + remote RAM = remote only",
                  s["host_mem_mb"] == 8192)
        finally:
            app.host_mem_total_mb = lambda: 31744

        # ---------- D. remote_host_ram_mb(providers) ----------
        print("== D. remote_host_ram_mb(providers) ==")
        now = time.time()
        fresh = {"last_seen": now - 60, "stats": {"ram_total_mb": 8192}}
        check("fresh node counted", app.remote_host_ram_mb({"n1": fresh}) == 8192)
        check("119s fresh counted",
              app.remote_host_ram_mb(
                  {"n1": {"last_seen": now - 119,
                          "stats": {"ram_total_mb": 100}}}) == 100)
        check("121s stale excluded",
              app.remote_host_ram_mb(
                  {"n1": {"last_seen": now - 121,
                          "stats": {"ram_total_mb": 100}}}) == 0)
        check("missing last_seen excluded",
              app.remote_host_ram_mb({"n1": {"stats": {"ram_total_mb": 100}}}) == 0)
        check("missing stats excluded",
              app.remote_host_ram_mb({"n1": {"last_seen": now}}) == 0)
        check("garbage ram string excluded",
              app.remote_host_ram_mb(
                  {"n1": {"last_seen": now,
                          "stats": {"ram_total_mb": "8192"}}}) == 0)
        check("negative ram excluded",
              app.remote_host_ram_mb(
                  {"n1": {"last_seen": now,
                          "stats": {"ram_total_mb": -5}}}) == 0)
        check("bool ram excluded",
              app.remote_host_ram_mb(
                  {"n1": {"last_seen": now,
                          "stats": {"ram_total_mb": True}}}) == 0)
        check("non-dict provider skipped",
              app.remote_host_ram_mb({"n1": "junk", "n2": fresh}) == 8192)
        check("None providers -> 0", app.remote_host_ram_mb(None) == 0)
        check("multi-node sum",
              app.remote_host_ram_mb(
                  {"n1": fresh,
                   "n2": {"last_seen": now - 10,
                          "stats": {"ram_total_mb": 4096}}}) == 12288)
        check("garbage providers arg never raises",
              app.remote_host_ram_mb(["x"]) == 0)

        # ---------- E. /api/instances wiring (source-level) ----------
        print("== E. wiring ==")
        SRC = open(APP).read()
        check("handler passes remote_instances",
              "remote_instances=remote_entries" in SRC)
        check("handler passes remote_host_ram_mb",
              "remote_host_ram_mb=remote_host_ram_mb(providers)" in SRC)
        check("remote_entries filter provider != local",
              'remote_entries = [e for e in out if e.get("provider") != "local"]'
              in SRC)
        check("fleet_resource_summary signature has both params",
              "def fleet_resource_summary(remote_instances=None, "
              "remote_host_ram_mb=0)" in SRC)
        check("staleness constant is 120s",
              "REMOTE_FLEET_STALE_S = 120" in SRC)
    finally:
        app.host_mem_total_mb = orig_host
        with app._resource_lock:
            app._resource_cache.clear()

    # ---------- F. dashboard contract (unchanged rendering) ----------
    print("== F. dashboard ==")
    DASH_SRC = open(HTML).read()
    _rescov = re.search(r"function updateResCov\(fr\) \{(.*?)\n\}", DASH_SRC, re.S)
    check("dash updateResCov renders same 4 keys",
          _rescov is not None and all(k in _rescov.group(1) for k in
                                      ("fr.mem_used_mb", "fr.host_mem_mb",
                                       "fr.cpu_pct", "fr.instances_seen")))
    check("dash resCov span", 'id="resCov"' in DASH_SRC)

    # ---------- G. _fleet_number ----------
    print("== G. _fleet_number ==")
    check("valid rounds to 1dp", app._fleet_number(100.04) == 100.0)
    check("string -> None", app._fleet_number("abc") is None)
    check("NaN -> None", app._fleet_number(float("nan")) is None)
    check("inf -> None", app._fleet_number(float("inf")) is None)
    check("negative -> None", app._fleet_number(-1.0) is None)
    check("over cap -> None", app._fleet_number(1000001.0) is None)
    check("None -> None", app._fleet_number(None) is None)

    print(f"\nPASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
