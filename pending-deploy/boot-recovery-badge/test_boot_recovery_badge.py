#!/usr/bin/env python3
"""Tests for boot-recovery pause surfacing (boot.degraded marker).

GAP: the watchdog's host-boot fleet recovery (main-node df-watchdog.sh
section 0, wing agent sw_boot_recovery) pauses the whole fleet start
when the binder pool is blocked or incomplete — but that pause was
log-only (one ALERT/WARN in the watchdog log). The dashboard grid kept
showing plain "stopped" cards while nothing could start, and the Eventos
log scrolled past it. The other farm-wide pauses (manager/docker/disk/
memory budgets) already surface as the red "Atención del sistema" banner
(global-health); boot recovery was the fifth global state with no
banner entry, and the wing node's equivalent never reached the Nodes
"needs attention" badge either.

FIX: both recoveries now write a boot.degraded marker (reason on the
first line, same convention as the other global markers) on their
paused paths, and clear it as soon as the pool is complete again, the
queue drains, or the boot window is missed — it never outlives the
pause. The manager adds ("boot", "boot.degraded") to
GLOBAL_WATCHDOG_MARKERS (banner label "Arranque"); the agent needs no
manager/dashboard change because selfwatch_state() already maps every
*.degraded file into the heartbeat degraded map. Both global-file
exclusion sets gain boot.degraded so an instance literally named
"boot" can never sweep the farm-wide marker. No agent VERSION bump:
this writes a marker file only — no new recovery action, no protocol
change, the manager's auto-login gate is untouched.

Covers (hermetic):
  A. manager.global_watchdog_health() — boot marker parsing + ordering
  B. source contracts — manager tuple + global set, dashboard label,
     agent global set, agent VERSION is 1.0.42 (bumped by input-coord-validation), watchdog
     write/clear references
  C. delete sweeps — an instance named "boot" cannot sweep boot.degraded
     (manager + agent)
  D. agent sw_boot_recovery() — stub -> pool-paused + marker + degraded
     map; echo-fail -> incomplete + marker; complete -> marker cleared +
     sentinel; window missed -> marker cleared; sentinel -> noop
  E. real df-watchdog.sh runs — incomplete pool -> marker; stub-blocked
     -> marker; complete pool -> marker cleared + boot.done
"""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
HTML = os.path.join(REPO, "manager", "static", "index.html")
WD = os.path.join(REPO, "waydroid-image", "df-watchdog.sh")
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


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def read1(path):
    try:
        with open(path) as f:
            return f.readline().strip()
    except OSError:
        return None


def main():
    print("boot-recovery-badge tests")
    try:
        app = load("brb_app", APP)
    except Exception as e:  # noqa: BLE001
        bad("import manager/app.py", str(e))
        app = None
    try:
        agent = load("brb_agent", AGENT)
    except Exception as e:  # noqa: BLE001
        bad("import wing/agent.py", str(e))
        agent = None

    # ---------- A. manager.global_watchdog_health() ----------
    print("== A. global_watchdog_health() boot marker ==")
    if app is not None:
        with tempfile.TemporaryDirectory() as st:
            # Fixture models a FIRING watchdog: a fresh heartbeat, so the
            # heartbeat-banner entry never leaks into marker assertions.
            with open(os.path.join(st, "heartbeat"), "w") as f:
                f.write("")
            h = app.global_watchdog_health(st_dir=st)
            check("no markers -> []", h == [], repr(h))
            write(os.path.join(st, "boot.degraded"),
                  "boot recovery paused: binder pool blocked by a non-empty "
                  "stub directory - manual cleanup needed\n")
            h = app.global_watchdog_health(st_dir=st)
            check("boot alone -> one boot entry",
                  len(h) == 1 and h[0]["key"] == "boot", repr(h))
            check("boot reason exact",
                  h[0]["reason"].startswith("boot recovery paused: binder "
                                            "pool blocked"), repr(h))
            for fn in ("manager.degraded", "docker.degraded",
                       "disk.degraded", "mem.degraded"):
                write(os.path.join(st, fn), "x\n")
            h = app.global_watchdog_health(st_dir=st)
            check("all five markers -> boot last",
                  [e["key"] for e in h] == ["manager", "docker", "disk",
                                            "memory", "boot"], repr(h))
            write(os.path.join(st, "boot.degraded"), "")
            h = app.global_watchdog_health(st_dir=st)
            boot = [e for e in h if e["key"] == "boot"][0]
            check("empty boot marker -> generic reason",
                  boot["reason"] == "boot recovery paused", repr(boot))
            os.remove(os.path.join(st, "boot.degraded"))
            os.mkdir(os.path.join(st, "boot.degraded"))
            h = app.global_watchdog_health(st_dir=st)
            check("directory boot marker skipped",
                  all(e["key"] != "boot" for e in h), repr(h))

    # ---------- B. source contracts ----------
    print("== B. source contracts ==")
    try:
        src = open(APP, encoding="utf-8").read()
    except OSError as e:
        bad("read app.py", str(e))
        src = ""
    check("GLOBAL_WATCHDOG_MARKERS has boot tuple",
          '("boot", "boot.degraded", "boot recovery paused")' in src)
    check("manager global set has boot.degraded",
          '"boot.degraded"' in src)
    try:
        html = open(HTML, encoding="utf-8").read()
    except OSError as e:
        bad("read index.html", str(e))
        html = ""
    check("dashboard labels boot as Arranque",
          "boot:'Arranque'" in html)
    try:
        wd = open(WD, encoding="utf-8").read()
    except OSError as e:
        bad("read df-watchdog.sh", str(e))
        wd = ""
    check("watchdog writes boot.degraded on stub-blocked",
          "binder pool blocked by a non-empty stub directory" in wd
          and 'boot.degraded' in wd)
    check("watchdog writes boot.degraded on pool-incomplete",
          "binder pool incomplete ($have/$BINDER_COUNT)" in wd)
    check("watchdog clears boot.degraded on boot.done",
          'rm -f "$ST/boot.degraded"' in wd)
    if agent is not None:
        check("agent VERSION is 1.0.44 (bumped by numeric-overflow-guard)",
              agent.VERSION == "1.0.45", repr(agent.VERSION))
        ag_src = open(AGENT, encoding="utf-8").read()
        check("agent global set has boot.degraded",
              '"boot.degraded"' in ag_src)
        check("agent writes boot.degraded on pool-paused",
              "boot recovery paused: binder pool blocked" in ag_src)
        check("agent writes boot.degraded on incomplete",
              "boot recovery paused: binder pool incomplete" in ag_src)

    # ---------- C. delete sweeps keep the global marker ----------
    print("== C. global marker survives a 'boot'-named delete ==")
    if app is not None:
        with tempfile.TemporaryDirectory() as st:
            os.environ["DF_ST"] = st
            write(os.path.join(st, "boot.degraded"), "global\n")
            try:
                n = app.drop_watchdog_state("boot")
            finally:
                os.environ.pop("DF_ST", None)
            check("manager: drop_watchdog_state('boot') keeps boot.degraded",
                  os.path.isfile(os.path.join(st, "boot.degraded")),
                  f"removed={n}")
    if agent is not None:
        with tempfile.TemporaryDirectory() as st:
            write(os.path.join(st, "boot.degraded"), "global\n")
            agent.drop_selfwatch_state("boot", st_dir=st)
            check("agent: drop_selfwatch_state('boot') keeps boot.degraded",
                  os.path.isfile(os.path.join(st, "boot.degraded")))

    # ---------- D. agent sw_boot_recovery() ----------
    print("== D. agent sw_boot_recovery() ==")
    if agent is not None:
        def run_ok(cmd, timeout=30, shell=False):
            return (0, "")

        def run_fail(cmd, timeout=30, shell=False):
            return (1, "")

        def fake_docker(*args, timeout=60):
            if args and args[0] == "ps":
                return (0, "")
            return (0, "")

        # D1: stub-blocked -> pool-paused + marker + degraded map
        with tempfile.TemporaryDirectory() as base:
            bfs = os.path.join(base, "binderfs")
            os.makedirs(bfs)
            write(os.path.join(bfs, "binder-control"), "")
            os.makedirs(os.path.join(bfs, "binder1"))
            write(os.path.join(bfs, "binder1", "junk"), "x")
            st = os.path.join(base, "st")
            sent = os.path.join(base, "sent")
            os.makedirs(sent, exist_ok=True)
            up = os.path.join(base, "uptime")
            write(up, "100.00 200.00\n")
            status = agent.sw_boot_recovery(
                st_dir=st, sentinel_dir=sent, uptime_path=up,
                binderfs=bfs, instances=[], docker_fn=fake_docker,
                run_fn=run_ok)
            check("stub -> pool-paused", status == "pool-paused",
                  repr(status))
            check("stub -> boot.degraded written",
                  read1(os.path.join(st, "boot.degraded")) is not None
                  and "blocked" in (read1(os.path.join(st, "boot.degraded"))
                                    or ""))
            deg = agent.selfwatch_state(st_dir=st)["degraded"]
            check("stub -> degraded map carries boot",
                  "boot" in deg, repr(deg))
            check("stub -> alert file written",
                  os.path.isfile(os.path.join(sent, "wing-boot.stub-alert")))

        # D2: echo-fail -> incomplete + marker
        with tempfile.TemporaryDirectory() as base:
            bfs = os.path.join(base, "binderfs")
            os.makedirs(bfs)
            write(os.path.join(bfs, "binder-control"), "")
            st = os.path.join(base, "st")
            sent = os.path.join(base, "sent")
            up = os.path.join(base, "uptime")
            write(up, "100.00 200.00\n")
            status = agent.sw_boot_recovery(
                st_dir=st, sentinel_dir=sent, uptime_path=up,
                binderfs=bfs, instances=[], docker_fn=fake_docker,
                run_fn=run_fail)
            check("echo-fail -> incomplete", status == "incomplete",
                  repr(status))
            check("incomplete -> boot.degraded written",
                  "incomplete (0/64)" in (read1(os.path.join(st, "boot.degraded"))
                                          or ""))

        # D3: complete -> marker cleared + sentinel + status complete
        with tempfile.TemporaryDirectory() as base:
            bfs = os.path.join(base, "binderfs")
            os.makedirs(bfs)
            write(os.path.join(bfs, "binder-control"), "")
            st = os.path.join(base, "st")
            write(os.path.join(st, "boot.degraded"), "stale\n")
            sent = os.path.join(base, "sent")
            up = os.path.join(base, "uptime")
            write(up, "100.00 200.00\n")
            status = agent.sw_boot_recovery(
                st_dir=st, sentinel_dir=sent, uptime_path=up,
                binderfs=bfs, instances=[], docker_fn=fake_docker,
                run_fn=run_ok)
            check("complete -> status complete", status == "complete",
                  repr(status))
            check("complete -> boot.degraded cleared",
                  not os.path.exists(os.path.join(st, "boot.degraded")))
            check("complete -> sentinel written",
                  os.path.isfile(os.path.join(sent, "wing-boot.done")))

        # D4: window missed -> marker cleared + sentinel
        with tempfile.TemporaryDirectory() as base:
            st = os.path.join(base, "st")
            write(os.path.join(st, "boot.degraded"), "stale\n")
            sent = os.path.join(base, "sent")
            up = os.path.join(base, "uptime")
            write(up, "99999.00 1.00\n")
            status = agent.sw_boot_recovery(
                st_dir=st, sentinel_dir=sent, uptime_path=up,
                binderfs=os.path.join(base, "binderfs"), instances=[],
                docker_fn=fake_docker, run_fn=run_ok)
            check("window missed -> noop-window", status == "noop-window",
                  repr(status))
            check("window missed -> boot.degraded cleared",
                  not os.path.exists(os.path.join(st, "boot.degraded")))

        # D5: sentinel exists -> noop + marker cleared
        with tempfile.TemporaryDirectory() as base:
            st = os.path.join(base, "st")
            write(os.path.join(st, "boot.degraded"), "stale\n")
            sent = os.path.join(base, "sent")
            write(os.path.join(sent, "wing-boot.done"), "")
            up = os.path.join(base, "uptime")
            write(up, "100.00 200.00\n")
            status = agent.sw_boot_recovery(
                st_dir=st, sentinel_dir=sent, uptime_path=up,
                binderfs=os.path.join(base, "binderfs"), instances=[],
                docker_fn=fake_docker, run_fn=run_ok)
            check("sentinel -> noop-sentinel", status == "noop-sentinel",
                  repr(status))
            check("sentinel -> boot.degraded cleared",
                  not os.path.exists(os.path.join(st, "boot.degraded")))

    # ---------- E. real df-watchdog.sh runs ----------
    print("== E. real watchdog boot_recovery() ==")
    def run_watchdog(case_dir, binderfs_setup):
        bindir = os.path.join(case_dir, "bin")
        os.makedirs(bindir, exist_ok=True)
        docker_sh = os.path.join(bindir, "docker")
        with open(docker_sh, "w") as f:
            f.write("#!/bin/sh\n"
                    "case \"$1\" in\n"
                    "  info) exit 0;;\n"
                    "  ps) exit 0;;\n"
                    "  start) exit 0;;\n"
                    "  *) exit 0;;\n"
                    "esac\n")
        os.chmod(docker_sh, 0o755)
        curl_sh = os.path.join(bindir, "curl")
        with open(curl_sh, "w") as f:
            f.write("#!/bin/sh\n"
                    "prev=\"\"\n"
                    "for a in \"$@\"; do\n"
                    "  if [ \"$prev\" = \"-o\" ]; then : > \"$a\"; fi\n"
                    "  prev=\"$a\"\n"
                    "done\n"
                    "echo 200\n"
                    "exit 0\n")
        os.chmod(curl_sh, 0o755)
        bfs = os.path.join(case_dir, "binderfs")
        os.makedirs(bfs, exist_ok=True)
        binderfs_setup(bfs)
        st = os.path.join(case_dir, "st")
        up = os.path.join(case_dir, "uptime")
        write(up, "100.00 200.00\n")
        env = dict(os.environ)
        env["PATH"] = bindir + ":" + env.get("PATH", "")
        env["DF_LOG"] = os.path.join(case_dir, "wd.log")
        env["DF_ST"] = st
        env["DF_INSTANCES"] = "rbx1"
        env["DF_PROC_UPTIME"] = up
        env["DF_BINDERFS"] = bfs
        env["DF_LOCKDIR"] = os.path.join(case_dir, "lock")
        env["DF_LOG_GLOB"] = os.path.join(case_dir, "nologs", "*-json.log")
        env["DF_CURL_BIN"] = curl_sh
        proc = subprocess.run(["bash", WD], env=env, capture_output=True,
                              text=True, timeout=120)
        return proc, st

    # E1: binder-control as a directory -> every write rejected -> incomplete
    with tempfile.TemporaryDirectory() as case:
        def setup_control_dir(bfs):
            os.makedirs(os.path.join(bfs, "binder-control"))
        proc, st = run_watchdog(case, setup_control_dir)
        check("watchdog incomplete -> exit 0", proc.returncode == 0,
              proc.stderr[-300:])
        check("watchdog incomplete -> boot.degraded written",
              os.path.isfile(os.path.join(st, "boot.degraded")))
        check("watchdog incomplete -> reason names pool incomplete",
              "incomplete (0/64)" in (read1(os.path.join(st, "boot.degraded"))
                                      or ""))

    # E2: non-empty stub dir -> stub-blocked marker
    with tempfile.TemporaryDirectory() as case:
        def setup_stub(bfs):
            write(os.path.join(bfs, "binder-control"), "")
            os.makedirs(os.path.join(bfs, "binder1"))
            write(os.path.join(bfs, "binder1", "junk"), "x")
        proc, st = run_watchdog(case, setup_stub)
        check("watchdog stub -> exit 0", proc.returncode == 0,
              proc.stderr[-300:])
        check("watchdog stub -> boot.degraded written",
              os.path.isfile(os.path.join(st, "boot.degraded")))
        check("watchdog stub -> reason names blocked stub",
              "blocked by a non-empty stub directory" in
              (read1(os.path.join(st, "boot.degraded")) or ""))

    # E3: healthy pool -> stale marker cleared + boot.done written
    with tempfile.TemporaryDirectory() as case:
        def setup_ok(bfs):
            write(os.path.join(bfs, "binder-control"), "")
        proc, st = run_watchdog(case, setup_ok)
        # pre-seed the stale marker, then run again: the second run's
        # pool-complete path must clear it (first run already completed
        # boot, so re-run with a fresh case but a pre-seeded marker)
        check("watchdog complete -> exit 0", proc.returncode == 0,
              proc.stderr[-300:])
        check("watchdog complete -> boot.done written",
              os.path.isfile(os.path.join(st, "boot.done")))
        check("watchdog complete -> boot.degraded absent",
              not os.path.exists(os.path.join(st, "boot.degraded")))
    with tempfile.TemporaryDirectory() as case:
        # stale marker + healthy pool, boot not yet done: run, then the
        # pool-complete path inside boot_recovery must clear it
        bindir = os.path.join(case, "bin")
        os.makedirs(bindir, exist_ok=True)
        docker_sh = os.path.join(bindir, "docker")
        with open(docker_sh, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(docker_sh, 0o755)
        curl_sh = os.path.join(bindir, "curl")
        with open(curl_sh, "w") as f:
            f.write("#!/bin/sh\nprev=\"\"\nfor a in \"$@\"; do "
                    "if [ \"$prev\" = \"-o\" ]; then : > \"$a\"; fi; "
                    "prev=\"$a\"; done\necho 200\nexit 0\n")
        os.chmod(curl_sh, 0o755)
        bfs = os.path.join(case, "binderfs")
        os.makedirs(bfs)
        write(os.path.join(bfs, "binder-control"), "")
        st = os.path.join(case, "st")
        write(os.path.join(st, "boot.degraded"), "stale paused\n")
        up = os.path.join(case, "uptime")
        write(up, "100.00 200.00\n")
        env = dict(os.environ)
        env["PATH"] = bindir + ":" + env.get("PATH", "")
        env["DF_LOG"] = os.path.join(case, "wd.log")
        env["DF_ST"] = st
        env["DF_INSTANCES"] = "rbx1"
        env["DF_PROC_UPTIME"] = up
        env["DF_BINDERFS"] = bfs
        env["DF_LOCKDIR"] = os.path.join(case, "lock")
        env["DF_LOG_GLOB"] = os.path.join(case, "nologs", "*-json.log")
        env["DF_CURL_BIN"] = curl_sh
        proc = subprocess.run(["bash", WD], env=env, capture_output=True,
                              text=True, timeout=120)
        check("watchdog stale-marker run -> exit 0",
              proc.returncode == 0, proc.stderr[-300:])
        check("watchdog stale marker cleared by pool-complete path",
              not os.path.exists(os.path.join(st, "boot.degraded")))

    print(f"PASS={PASS} FAIL={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
