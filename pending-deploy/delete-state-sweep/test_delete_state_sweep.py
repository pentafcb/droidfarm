#!/usr/bin/env python3
"""Tests for delete-state-sweep: deleting an instance must not leave
stale per-instance state behind for a recreated instance to inherit.

GAP: delete removed the container, volumes, and instances.json entry,
but outcome/marker state keyed by instance name survived:
  - roblox_login_results.json entry -> recreated instance showed the
    dead instance's login badge until a new attempt overwrote it.
  - /tmp/df-watchdog/<name>.degraded / .roblox_drift / .ev windows /
    .helper-missing-* / .dw-timeout.* -> recreated instance showed the
    dead instance's "needs attention" badge (degraded has priority over
    the setup badge) and drift badge through the whole boot window,
    because the watchdog only clears markers once ALL checks go green.
  - the wing agent's selfwatch dir had the same problem node-side.
The delete paths now sweep all of it. Saved accounts are deliberately
NOT swept: credentials are never deleted by the delete path.

Covers (hermetic):
  A. manager.drop_login_result() — drop/missing/invalid/corrupt/perms
  B. manager.drop_watchdog_state() — full file set, prefix guard,
     global-file guard, missing dir, invalid name
  C. manager.drop_resource_cache() + _stop_h264_broadcaster()
  D. manager do_DELETE wiring — local + remote blocks, no account touch
  E. agent.drop_selfwatch_state() — full file set, prefix guard,
     global-file guard, invalid name, missing dir
  F. agent delete wiring + selfwatch_state() cross-check
"""
import importlib.util
import inspect
import json
import os
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
AGENT = os.path.join(REPO, "wing", "agent.py")

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


def load_mod(path, label):
    spec = importlib.util.spec_from_file_location(label, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def touch(d, fn, content="x\n"):
    p = os.path.join(d, fn)
    with open(p, "w") as f:
        f.write(content)
    return p


def main():
    print("delete-state-sweep tests")
    try:
        app = load_mod(APP, "df_app")
    except Exception as e:  # noqa: BLE001
        bad("import manager/app.py", str(e))
        app = None
    try:
        agent = load_mod(AGENT, "df_agent")
    except Exception as e:  # noqa: BLE001
        bad("import wing/agent.py", str(e))
        agent = None

    # ---- A. drop_login_result -------------------------------------------
    if app is not None:
        tmp = tempfile.mkdtemp()
        resf = os.path.join(tmp, "roblox_login_results.json")
        app.LOGIN_RESULTS_FILE = resf
        with open(resf, "w") as f:
            json.dump({"rbx1": {"ok": True, "ts": 1, "reason": "",
                                 "by": "manual", "attempts": 3},
                       "rbx2": {"ok": False, "ts": 2, "reason": "no creds",
                                "by": "manual", "attempts": 1}}, f)
        check("login: drop existing returns True",
              app.drop_login_result("rbx1") is True)
        with open(resf) as f:
            d = json.load(f)
        check("login: entry gone, sibling kept",
              "rbx1" not in d and d.get("rbx2", {}).get("attempts") == 1)
        check("login: saved file is 600",
              stat.S_IMODE(os.stat(resf).st_mode) == 0o600)
        check("login: drop missing returns False",
              app.drop_login_result("rbx9") is False)
        check("login: drop invalid name returns False",
              app.drop_login_result("bad name!") is False)
        with open(resf, "w") as f:
            f.write("{not json")
        check("login: corrupt file never raises, returns False",
              app.drop_login_result("rbx2") is False)
        src = inspect.getsource(app.drop_login_result)
        check("login: drop never touches accounts",
              "ACCOUNTS_FILE" not in src and "save_accounts" not in src
              and "load_accounts" not in src)

    # ---- B. drop_watchdog_state ------------------------------------------
    if app is not None:
        st = tempfile.mkdtemp()
        old_df_st = os.environ.get("DF_ST")
        os.environ["DF_ST"] = st
        try:
            for fn in ("rbx1.degraded", "rbx1.roblox_drift",
                       "rbx1.robloxver", "rbx1.sessionstart.ts",
                       "rbx1.restart.ev", "rbx1.helper-screencap-loop.sh.ev",
                       "rbx1.helper-missing-screencap_loop_sh.2026-10-25",
                       "rbx1.dw-timeout.2026-10-25", "rbx1.bootstart.ev"):
                touch(st, fn, "reason line\n")
            touch(st, "rbx10.degraded", "other instance\n")
            touch(st, "notes.txt", "unrelated\n")
            touch(st, "docker.degraded", "global docker\n")
            touch(st, "disk.prune.ev", "global\n")
            touch(st, "mgr.restart.ev", "global\n")
            n = app.drop_watchdog_state("rbx1")
            check("watchdog: removes all 9 per-instance files", n == 9,
                  f"got {n}")
            gone = all(not os.path.exists(os.path.join(st, fn)) for fn in
                       ("rbx1.degraded", "rbx1.roblox_drift",
                        "rbx1.robloxver", "rbx1.sessionstart.ts",
                        "rbx1.restart.ev",
                        "rbx1.helper-screencap-loop.sh.ev",
                        "rbx1.helper-missing-screencap_loop_sh.2026-10-25",
                        "rbx1.dw-timeout.2026-10-25", "rbx1.bootstart.ev"))
            check("watchdog: per-instance files gone", gone)
            check("watchdog: rbx10 prefix collision survives",
                  os.path.exists(os.path.join(st, "rbx10.degraded")))
            check("watchdog: unrelated file survives",
                  os.path.exists(os.path.join(st, "notes.txt")))
            check("watchdog: global docker.degraded survives",
                  os.path.exists(os.path.join(st, "docker.degraded")))
            check("watchdog: global disk.prune.ev survives",
                  os.path.exists(os.path.join(st, "disk.prune.ev")))
            check("watchdog: global mgr.restart.ev survives",
                  os.path.exists(os.path.join(st, "mgr.restart.ev")))
            check("watchdog: second sweep removes 0",
                  app.drop_watchdog_state("rbx1") == 0)
            check("watchdog: docker-named instance sweeps nothing",
                  app.drop_watchdog_state("docker") == 0)
            check("watchdog: docker globals still intact after that",
                  os.path.exists(os.path.join(st, "docker.degraded"))
                  and os.path.exists(os.path.join(st, "disk.prune.ev")))
            check("watchdog: invalid name returns 0",
                  app.drop_watchdog_state("") == 0)
            os.environ["DF_ST"] = os.path.join(st, "does-not-exist")
            check("watchdog: missing dir returns 0, no raise",
                  app.drop_watchdog_state("rbx1") == 0)
        finally:
            if old_df_st is None:
                os.environ.pop("DF_ST", None)
            else:
                os.environ["DF_ST"] = old_df_st

    # ---- C. resource cache + h264 broadcaster ---------------------------
    if app is not None:
        with app._resource_lock:
            app._resource_cache["zzzdel1"] = {"mem_used_mb": 1.0,
                                               "cpu_pct": 1.0, "ts": 0}
        app.drop_resource_cache("zzzdel1")
        check("resources: dropped entry gone",
              "zzzdel1" not in app._resource_cache)
        try:
            app.drop_resource_cache("zzzdel-missing")
            check("resources: drop missing never raises", True)
        except Exception as e:  # noqa: BLE001
            check("resources: drop missing never raises", False, str(e))

        class FakeB:
            def __init__(self):
                self.stopped = False
            def stop(self):
                self.stopped = True

        fb = FakeB()
        with app._lock:
            app._h264_broadcasters[("local", "zzzdel1")] = fb
        app._stop_h264_broadcaster("zzzdel1", "local")
        check("h264: broadcaster popped + stopped",
              ("local", "zzzdel1") not in app._h264_broadcasters
              and fb.stopped)

        class RaisingB:
            def stop(self):
                raise RuntimeError("boom")

        with app._lock:
            app._h264_broadcasters[("local", "zzzdel2")] = RaisingB()
        try:
            app._stop_h264_broadcaster("zzzdel2", "local")
            check("h264: raising stop never raises, still popped",
                  ("local", "zzzdel2") not in app._h264_broadcasters)
        except Exception as e:  # noqa: BLE001
            check("h264: raising stop never raises, still popped",
                  False, str(e))
        app._stop_h264_broadcaster("zzzdel-missing", "local")
        check("h264: stop missing is a no-op", True)

    # ---- D. do_DELETE wiring ---------------------------------------------
    if app is not None:
        src = open(APP).read()
        i_del = src.find("    def do_DELETE(self):")
        i_next = src.find("    def _create_instance(self):", i_del)
        local_block = src[i_del:i_next]
        check("wiring: local delete sweeps login result",
              "drop_login_result(name)" in local_block)
        check("wiring: local delete sweeps watchdog state",
              "drop_watchdog_state(name)" in local_block)
        check("wiring: local delete drops resource cache",
              "drop_resource_cache(name)" in local_block)
        check("wiring: local delete stops h264 broadcaster",
              '_stop_h264_broadcaster(name, "local")' in local_block)
        check("wiring: local delete never touches accounts",
              "ACCOUNTS_FILE" not in local_block
              and "save_accounts" not in local_block
              and "load_accounts" not in local_block)
        i_remote = local_block.find("drop_remote_status(prov, name)")
        remote_block = local_block[i_remote:i_remote + 800]
        check("wiring: remote delete sweeps login result",
              "drop_login_result(name)" in remote_block)
        check("wiring: remote delete stops h264 broadcaster",
              "_stop_h264_broadcaster(name, prov)" in remote_block)
        check("wiring: remote delete does NOT sweep node watchdog state",
              "drop_watchdog_state(name)" not in remote_block)

    # ---- E. agent drop_selfwatch_state ------------------------------------
    if agent is not None:
        st = tempfile.mkdtemp()
        for fn in ("srt1.degraded", "srt1.roblox_drift",
                   "srt1.roblox_drift_seen", "srt1.sessionstart.ts",
                   "srt1.bootstart.ev", "srt1.helper-x.ev"):
            touch(st, fn, "reason\n")
        touch(st, "srt10.degraded", "other\n")
        touch(st, "disk.degraded", "global\n")
        touch(st, "loop.ensure.ev", "global\n")
        touch(st, "agent.http.degraded", "global\n")
        n = agent.drop_selfwatch_state("srt1", st_dir=st)
        check("agent: removes all 6 per-instance files", n == 6,
              f"got {n}")
        check("agent: per-instance files gone",
              all(not os.path.exists(os.path.join(st, fn)) for fn in
                  ("srt1.degraded", "srt1.roblox_drift",
                   "srt1.roblox_drift_seen", "srt1.sessionstart.ts",
                   "srt1.bootstart.ev", "srt1.helper-x.ev")))
        check("agent: srt10 prefix collision survives",
              os.path.exists(os.path.join(st, "srt10.degraded")))
        check("agent: global disk.degraded survives",
              os.path.exists(os.path.join(st, "disk.degraded")))
        check("agent: global loop.ensure.ev survives",
              os.path.exists(os.path.join(st, "loop.ensure.ev")))
        check("agent: global agent.http.degraded survives",
              os.path.exists(os.path.join(st, "agent.http.degraded")))
        check("agent: disk-named instance sweeps nothing",
              agent.drop_selfwatch_state("disk", st_dir=st) == 0)
        check("agent: invalid name returns 0",
              agent.drop_selfwatch_state("bad name!", st_dir=st) == 0)
        check("agent: missing dir returns 0, no raise",
              agent.drop_selfwatch_state(
                  "srt1", st_dir=os.path.join(st, "nope")) == 0)

    # ---- F. agent delete wiring + selfwatch cross-check -------------------
    if agent is not None:
        src = open(AGENT).read()
        i_del = src.find("def delete_instance_linux")
        # Slice the whole delete function (to the next top-level def),
        # not a fixed 900-char window: the t170 sink gate + its comment
        # pushed the drop_selfwatch_state call past offset 900 while
        # staying inside this function.
        i_next = src.find("\ndef ", i_del + 10)
        del_block = src[i_del:i_next if i_next != -1 else len(src)]
        check("agent wiring: delete calls drop_selfwatch_state",
              "drop_selfwatch_state(name)" in del_block)
        st = tempfile.mkdtemp()
        touch(st, "srt9.degraded", "wedged reason\n")
        touch(st, "recoveries.json", '{"total": 5}')
        before = agent.selfwatch_state(st_dir=st)
        check("agent x-check: selfwatch reports srt9 degraded",
              before["degraded"].get("srt9") == "wedged reason"
              and before["recoveries"] == 5)
        agent.drop_selfwatch_state("srt9", st_dir=st)
        after = agent.selfwatch_state(st_dir=st)
        check("agent x-check: sweep clears the heartbeat map",
              "srt9" not in after["degraded"]
              and after["recoveries"] == 5)
        check("agent x-check: recoveries total untouched",
              json.load(open(os.path.join(st, "recoveries.json")))["total"]
              == 5)

    print(f"PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
