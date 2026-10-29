#!/usr/bin/env python3
"""Tests for watchdog-heartbeat-banner: a dead watchdog minute-cron must
show in the dashboard system banner, not just in verify-live.sh.

GAP: the watchdog touches $ST/heartbeat on every executed run (after the
lock is held), but the dashboard banner only rendered marker files
(manager/docker/disk/memory/boot). A dead cron — crontab wiped, cron
daemon down, script broken before the lock — means NO auto-recovery at
all, yet the dashboard showed green cards: markers report trips, never
the absence of the watcher. Only verify-live.sh (run from the repo
machine) would notice, and nobody runs it on a schedule.

FIX: manager/global_watchdog_health() appends a trailing {"key":
"watchdog"} entry when the state dir exists but the heartbeat is missing
(never fired) or older than WATCHDOG_HEARTBEAT_MAX_AGE (600s = 10 missed
minute-runs, deliberately looser than verify-live's 300s WARN so one slow
run never flashes a persistent banner). Trips first, meta-state last, so
the marker ordering contract is unchanged. A missing state dir stays []
(fresh install is indistinguishable there, and the old "missing dir ->
[]" contract holds). Dashboard GLOBAL_HEALTH_LABELS gains
watchdog:'Watchdog'. Wing nodes need no equivalent: a dead agent stops
heartbeating and the node-offline badge already covers it. No watchdog
script change (it already touches the heartbeat), no agent change, no
VERSION bump: surfacing-only.

Covers (hermetic):
  A. manager.global_watchdog_health() — fresh/missing/stale/boundary
     heartbeats, marker+watchdog ordering, missing dir, heartbeat-as-dir,
     never-raises
  B. source contracts — threshold constant, dashboard label, watchdog
     script still touches the heartbeat, /api/instances wiring intact
"""
import importlib.util
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
HTML = os.path.join(REPO, "manager", "static", "index.html")
WD = os.path.join(REPO, "waydroid-image", "df-watchdog.sh")

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


def touch(path, age_s=0):
    with open(path, "w") as f:
        f.write("")
    if age_s:
        now = time.time()
        os.utime(path, (now - age_s, now - age_s))


def main():
    print("watchdog-heartbeat-banner tests")
    try:
        app = load("whb_app", APP)
    except Exception as e:  # noqa: BLE001
        bad("import manager/app.py", str(e))
        app = None

    # ---------- A. global_watchdog_health() heartbeat behavior ----------
    print("== A. heartbeat behavior ==")
    if app is not None:
        check("threshold is 600s",
              app.WATCHDOG_HEARTBEAT_MAX_AGE == 600,
              repr(getattr(app, "WATCHDOG_HEARTBEAT_MAX_AGE", None)))

        with tempfile.TemporaryDirectory() as st:
            # fresh heartbeat, no markers -> []
            touch(os.path.join(st, "heartbeat"))
            h = app.global_watchdog_health(st_dir=st)
            check("fresh heartbeat + no markers -> []", h == [], repr(h))

            # missing heartbeat (dir exists) -> watchdog entry, never-fired
            os.remove(os.path.join(st, "heartbeat"))
            h = app.global_watchdog_health(st_dir=st)
            check("missing heartbeat -> one watchdog entry",
                  len(h) == 1 and h[0]["key"] == "watchdog", repr(h))
            check("missing heartbeat reason names never fired",
                  "never" in h[0]["reason"], repr(h))

            # stale heartbeat (601s) -> watchdog entry with age
            touch(os.path.join(st, "heartbeat"), age_s=601)
            h = app.global_watchdog_health(st_dir=st)
            check("stale heartbeat -> one watchdog entry",
                  len(h) == 1 and h[0]["key"] == "watchdog", repr(h))
            check("stale reason carries age",
                  (lambda m: m is not None and int(m.group(1)) >= 600)(
                      re.search(r"heartbeat (\d+)s ago", h[0]["reason"])),
                  repr(h))

            # boundary: 599s fresh -> no entry
            touch(os.path.join(st, "heartbeat"), age_s=599)
            h = app.global_watchdog_health(st_dir=st)
            check("599s heartbeat -> []", h == [], repr(h))

            # exactly at threshold is fresh (strict >), 601 stale — the
            # 599/601 pair brackets the boundary without a timing race
            touch(os.path.join(st, "heartbeat"), age_s=601)
            h = app.global_watchdog_health(st_dir=st)
            check("601s heartbeat -> watchdog entry",
                  len(h) == 1 and h[0]["key"] == "watchdog", repr(h))

            # marker + stale heartbeat -> marker first, watchdog last
            with open(os.path.join(st, "disk.degraded"), "w") as f:
                f.write("disk auto-prune paused\n")
            h = app.global_watchdog_health(st_dir=st)
            check("marker + stale heartbeat -> marker first, watchdog last",
                  [e["key"] for e in h] == ["disk", "watchdog"], repr(h))

            # fresh heartbeat clears the watchdog entry, marker stays
            touch(os.path.join(st, "heartbeat"))
            h = app.global_watchdog_health(st_dir=st)
            check("fresh heartbeat clears watchdog entry, marker stays",
                  [e["key"] for e in h] == ["disk"], repr(h))

            # heartbeat as a directory -> treated as missing (fail closed)
            os.remove(os.path.join(st, "heartbeat"))
            os.mkdir(os.path.join(st, "heartbeat"))
            h = app.global_watchdog_health(st_dir=st)
            check("heartbeat-as-dir -> watchdog entry",
                  any(e["key"] == "watchdog" for e in h), repr(h))

        # missing state dir -> [] (old contract holds, no watchdog entry)
        h = app.global_watchdog_health(
            st_dir="/nonexistent-df-st-xyz-watchdog-hb")
        check("missing dir -> []", h == [], repr(h))

        # DF_ST env default path honored (no st_dir arg)
        with tempfile.TemporaryDirectory() as st:
            os.environ["DF_ST"] = st
            touch(os.path.join(st, "heartbeat"), age_s=601)
            h = app.global_watchdog_health()
            check("DF_ST default honored, stale -> watchdog entry",
                  len(h) == 1 and h[0]["key"] == "watchdog", repr(h))
            os.environ.pop("DF_ST", None)

    # ---------- B. source contracts ----------
    print("== B. source contracts ==")
    try:
        src = open(APP, encoding="utf-8").read()
    except OSError as e:
        bad("read app.py", str(e))
        src = ""
    check("signature unchanged (st_dir only)",
          re.search(r"def global_watchdog_health\(st_dir=None\):", src)
          is not None)
    check("GET /api/instances emits watchdog_global",
          '"watchdog_global": global_watchdog_health()' in src)
    check("heartbeat filename referenced",
          '"heartbeat"' in src)
    try:
        html = open(HTML, encoding="utf-8").read()
    except OSError as e:
        bad("read index.html", str(e))
        html = ""
    check("dashboard labels watchdog",
          "watchdog:'Watchdog'" in html)
    try:
        wd = open(WD, encoding="utf-8").read()
    except OSError as e:
        bad("read df-watchdog.sh", str(e))
        wd = ""
    check("watchdog still touches heartbeat",
          'touch "$ST/heartbeat"' in wd)

    print(f"PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
