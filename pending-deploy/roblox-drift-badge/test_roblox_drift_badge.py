#!/usr/bin/env python3
"""Tests for the Roblox version-drift dashboard badge.

The v6 watchdog pin guard detected drift but only logged it; Penta checks the
dashboard, not logs. Now df-watchdog.sh writes <instance>.roblox_drift
(reason on first line, same convention as .degraded), the manager surfaces
roblox_drift/roblox_drift_reason on /api/instances (local branch), and the
dashboard shows an amber "Roblox actualizado" badge with the reason tooltip.

Covers:
  A. real df-watchdog.sh against fake docker: drift -> marker written (reason
     carries versions), dedup keeps one ALERT per version, new version -> new
     ALERT + marker refresh, back-on-pinned -> marker + state removed,
     package absent / dumpsys fail / unbooted / container down -> no marker,
     section-6 degraded auto-clear never touches the drift marker
  B. manager.instance_health() drift marker parsing
  C. /api/instances contract: local emits roblox_drift/_reason, remote False/None
  D. dashboard contract: CSS, badge markup, priority, Spanish label, no auto-refresh
  E. regressions: watchdog-v6 40/40, instance-health 17/17
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
HTML = os.path.join(REPO, "manager", "static", "index.html")
WD = os.path.join(REPO, "waydroid-image", "df-watchdog.sh")
V6_TEST = os.path.join(REPO, "pending-deploy", "watchdog-v6", "test_watchdog_v6.sh")
IH_TEST = os.path.join(REPO, "pending-deploy", "instance-health",
                        "test_instance_health.py")

PASS = FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok: {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL: {name} {detail}")


def load_app():
    spec = importlib.util.spec_from_file_location("df_app_drift", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- A. watchdog
FAKE_DOCKER = r"""#!/bin/bash
sub=$1; shift
case "$sub" in
  ps)      cat "$T/docker_ps" 2>/dev/null ;;
  inspect) cat "$T/docker_started_at" 2>/dev/null ;;
  start|restart) exit 0 ;;
  image|builder) exit 0 ;;
  exec)
    cname=$1; shift
    case "$1" in
      pgrep) grep -qx "$3" "$T/pgrep_dead" 2>/dev/null && exit 1 || exit 0 ;;
      waydroid)
        case "$*" in *getprop*) echo 1; exit 0;; esac
        if [ -f "$T/roblox_fail" ]; then exit 1; fi
        if [ -f "$T/roblox_absent" ]; then
          echo "Unable to find package com.roblox.client"; exit 0
        fi
        v=2.740.931; [ -f "$T/roblox_version" ] && v=$(cat "$T/roblox_version")
        printf 'Package [com.roblox.client]:\n  versionCode=1234567\n  versionName=%s\n' "$v"
        exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
"""
FAKE_DF = r"""#!/bin/bash
u=$(cat "$T/df_use" 2>/dev/null || echo 70)
printf 'Filesystem     1K-blocks      Used Available Use%% Mounted on\n/dev/sda1      100000000  92000000   8000000  %s%% /var/lib/docker\n' "$u"
"""
FAKE_CURL = "#!/bin/bash\necho 200; exit 0\n"
FAKE_SYSTEMCTL = "#!/bin/bash\nexit 0\n"


class Rig:
    def __init__(self, t):
        self.t = t
        self.bin = os.path.join(t, "bin")
        self.st = os.path.join(t, "st")
        self.vol = os.path.join(t, "vol")
        self.log = os.path.join(t, "watchdog.log")
        os.makedirs(self.bin)
        for name, body in (("docker", FAKE_DOCKER), ("df", FAKE_DF),
                           ("curl", FAKE_CURL), ("systemctl", FAKE_SYSTEMCTL)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)

    def setup(self):
        for d in (self.st, self.vol):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d)
        open(self.log, "w").close()
        for f in ("roblox_fail", "roblox_absent", "roblox_version",
                  "docker_ps", "df_use", "pgrep_dead"):
            p = os.path.join(self.t, f)
            if os.path.exists(p):
                os.remove(p)
        with open(os.path.join(self.t, "docker_ps"), "w") as f:
            f.write("wd-rbx1\n")
        with open(os.path.join(self.t, "df_use"), "w") as f:
            f.write("70")
        open(os.path.join(self.t, "pgrep_dead"), "w").close()
        d = os.path.join(self.vol, "wd-rbx1-data", "_data", "data", "media",
                         "0", "jerusalem")
        os.makedirs(d, exist_ok=True)
        for leaf in ("booted", "live.png"):
            open(os.path.join(d, leaf), "w").close()

    def run(self):
        env = dict(os.environ, DF_ST=self.st, DF_LOG=self.log,
                   DF_INSTANCES="rbx1", DF_VOLBASE=self.vol,
                   DF_PINNED_ROBLOX="2.740.931", T=self.t,
                   PATH=self.bin + ":/usr/bin:/bin")
        subprocess.run(["bash", WD], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def read_log(self):
        with open(self.log) as f:
            return f.read()

    def marker(self):
        p = os.path.join(self.st, "rbx1.roblox_drift")
        return open(p).read() if os.path.isfile(p) else None

    def alert_count(self):
        return self.read_log().count("ALERT rbx1 Roblox client version DRIFT")


print("== A. watchdog drift marker ==")
with tempfile.TemporaryDirectory() as t:
    rig = Rig(t)

    rig.setup(); rig.run()
    if rig.marker() is None and not os.path.exists(
            os.path.join(rig.st, "rbx1.robloxver")) and rig.alert_count() == 0:
        ok("pinned: silent, no marker, no state file")
    else:
        bad("pinned: silent, no marker, no state file", repr(rig.marker()))

    rig.setup()
    with open(os.path.join(t, "roblox_version"), "w") as f:
        f.write("2.741.100")
    rig.run()
    m = rig.marker()
    if m and "2.741.100" in m.splitlines()[0] and "pinned 2.740.931" in m \
            and rig.alert_count() == 1:
        ok("drift: marker written, reason carries versions, one ALERT")
    else:
        bad("drift: marker/ALERT", repr(m))
    st = open(os.path.join(rig.st, "rbx1.robloxver")).read().strip()
    if st == "2.741.100":
        ok("drift: state file records version")
    else:
        bad("drift: state file records version", repr(st))

    rig.run()  # same drift again
    if rig.alert_count() == 1 and rig.marker() is not None:
        ok("repeat drift: ALERT deduped, marker kept")
    else:
        bad("repeat drift: dedup", rig.alert_count())

    with open(os.path.join(t, "roblox_version"), "w") as f:
        f.write("2.742.1")
    rig.run()
    m = rig.marker()
    if rig.alert_count() == 2 and m and "2.742.1" in m.splitlines()[0]:
        ok("new drifted version: new ALERT + marker refreshed")
    else:
        bad("new drifted version", repr(m))

    os.remove(os.path.join(t, "roblox_version"))  # back on pinned
    rig.run()
    log = rig.read_log()
    if rig.marker() is None and not os.path.exists(
            os.path.join(rig.st, "rbx1.robloxver")) \
            and "back on pinned build 2.740.931" in log:
        ok("back on pinned: marker + state removed, logged")
    else:
        bad("back on pinned", repr(rig.marker()))

    rig.setup()
    open(os.path.join(t, "roblox_absent"), "w").close()
    with open(os.path.join(t, "roblox_version"), "w") as f:
        f.write("2.741.100")
    rig.run()
    if rig.marker() is None and rig.alert_count() == 0:
        ok("package absent: silent skip, no marker")
    else:
        bad("package absent", repr(rig.marker()))

    rig.setup()
    open(os.path.join(t, "roblox_fail"), "w").close()
    with open(os.path.join(t, "roblox_version"), "w") as f:
        f.write("2.741.100")
    rig.run()
    if rig.marker() is None:
        ok("dumpsys failure: no marker")
    else:
        bad("dumpsys failure", repr(rig.marker()))

    rig.setup()  # unbooted: no booted marker, drifted client
    os.remove(os.path.join(rig.vol, "wd-rbx1-data", "_data", "data",
                           "media", "0", "jerusalem", "booted"))
    with open(os.path.join(t, "roblox_version"), "w") as f:
        f.write("2.741.100")
    rig.run()
    if rig.marker() is None:
        ok("unbooted instance: drift check skipped")
    else:
        bad("unbooted instance", repr(rig.marker()))

    rig.setup()  # container down
    open(os.path.join(t, "docker_ps"), "w").close()
    with open(os.path.join(t, "roblox_version"), "w") as f:
        f.write("2.741.100")
    rig.run()
    if rig.marker() is None:
        ok("container down: drift check skipped")
    else:
        bad("container down", repr(rig.marker()))

    # section-6 degraded auto-clear must not drop the drift marker
    rig.setup()
    with open(os.path.join(t, "roblox_version"), "w") as f:
        f.write("2.741.100")
    rig.run()
    with open(os.path.join(rig.st, "rbx1.degraded"), "w") as f:
        f.write("old reason\n")
    open(os.path.join(t, "roblox_version"), "w").close()  # back on pinned
    rig.run()  # all green now: degraded auto-clears, drift marker gone too
    src = open(WD).read()
    m6 = re.search(r"# 6\. degraded auto-clear.*?\n(.*?)\n\}", src, re.S)
    if m6 and "roblox_drift" not in m6.group(1):
        ok("section-6 auto-clear never references the drift marker")
    else:
        bad("section-6 auto-clear references drift marker")
    # and while still drifted the marker survives a green run
    rig.setup()
    with open(os.path.join(t, "roblox_version"), "w") as f:
        f.write("2.741.100")
    rig.run()
    with open(os.path.join(rig.st, "rbx1.degraded"), "w") as f:
        f.write("old reason\n")
    rig.run()
    if rig.marker() is not None and not os.path.exists(
            os.path.join(rig.st, "rbx1.degraded")):
        ok("drifted + green run: degraded cleared, drift marker kept")
    else:
        bad("drifted + green run", repr(rig.marker()))

# ------------------------------------------------- B. manager instance_health
print("== B. instance_health() drift fields ==")
with tempfile.TemporaryDirectory() as st:
    os.environ["DF_ST"] = st
    try:
        app = load_app()
    except Exception as e:  # noqa: BLE001
        bad("import manager/app.py", str(e))
        app = None
    if app is not None:
        h = app.instance_health("rbx1")
        if h == {"degraded": False, "reason": None,
                 "drift": False, "drift_reason": None}:
            ok("no markers -> clean health dict")
        else:
            bad("no markers -> clean health dict", repr(h))

        with open(os.path.join(st, "rbx1.roblox_drift"), "w") as f:
            f.write("Roblox client 2.741.100 installed (pinned 2.740.931) - "
                    "no auto-remediation\n")
        h = app.instance_health("rbx1")
        if h["drift"] is True and h["drift_reason"] == \
                "Roblox client 2.741.100 installed (pinned 2.740.931) - " \
                "no auto-remediation" and h["degraded"] is False:
            ok("drift marker -> drift True, reason passed through")
        else:
            bad("drift marker -> drift True", repr(h))

        open(os.path.join(st, "rbx2.roblox_drift"), "w").close()
        h = app.instance_health("rbx2")
        if h["drift"] is True and h["drift_reason"] == \
                "Roblox client drifted off pinned build":
            ok("empty drift marker -> generic reason")
        else:
            bad("empty drift marker", repr(h))

        with open(os.path.join(st, "rbx3.roblox_drift"), "w") as f:
            f.write("first line\nsecond line\n")
        h = app.instance_health("rbx3")
        if h["drift"] is True and h["drift_reason"] == "first line":
            ok("multiline drift marker -> first line only")
        else:
            bad("multiline drift marker", repr(h))

        with open(os.path.join(st, "rbx4.roblox_drift"), "w") as f:
            f.write("x" * 500 + "\n")
        h = app.instance_health("rbx4")
        if h["drift"] is True and len(h["drift_reason"]) == 200:
            ok("long drift reason truncated to 200 chars")
        else:
            bad("long drift reason", repr(h))

        os.mkdir(os.path.join(st, "rbx5.roblox_drift"))
        h = app.instance_health("rbx5")
        if h["drift"] is False and h["drift_reason"] is None:
            ok("drift path is a directory -> no drift")
        else:
            bad("drift path is a directory", repr(h))

        h = app.instance_health("../../etc/passwd")
        if h["drift"] is False:
            ok("path traversal in name sanitized")
        else:
            bad("path traversal in name sanitized", repr(h))

        with open(os.path.join(st, "rbx6.degraded"), "w") as f:
            f.write("wedged\n")
        with open(os.path.join(st, "rbx6.roblox_drift"), "w") as f:
            f.write("drifted\n")
        h = app.instance_health("rbx6")
        if h["degraded"] is True and h["drift"] is True \
                and h["reason"] == "wedged" and h["drift_reason"] == "drifted":
            ok("both markers coexist independently")
        else:
            bad("both markers coexist", repr(h))

    os.environ["DF_ST"] = os.path.join(st, "does-not-exist")
    try:
        h = app.instance_health("rbx1")
        if h == {"degraded": False, "reason": None,
                 "drift": False, "drift_reason": None}:
            ok("missing state dir -> clean, no raise")
        else:
            bad("missing state dir -> clean, no raise", repr(h))
    except Exception as e:  # noqa: BLE001
        bad("missing state dir raised", str(e))

# ------------------------------------------------- C. /api/instances contract
print("== C. /api/instances contract ==")
src = open(APP).read()
if '"roblox_drift": health["drift"]' in src and \
        '"roblox_drift_reason": health["drift_reason"]' in src:
    ok("local branch emits roblox_drift/_reason from instance_health()")
else:
    bad("local branch emits roblox_drift/_reason")
if '"roblox_drift": clean_agent_drift(rs.get("roblox_drift"))' in src and \
        "clean_agent_drift_reason(" in src and \
        'rs.get("roblox_drift_reason")' in src:
    ok("remote branch sanitizes agent drift (agent >=1.0.5; older -> False/None)")
else:
    bad("remote branch sanitizes agent drift")

# ------------------------------------------------- D. dashboard contract
print("== D. dashboard contract ==")
html = open(HTML).read()
if ".setup.drift" in html and "var(--warn)" in html:
    ok("drift CSS rule present (amber)")
else:
    bad("drift CSS rule present")
if "i.roblox_drift" in html and "setup drift" in html \
        and "i.roblox_drift_reason" in html and "ri-refresh-line" in html:
    ok("card renders drift badge with reason tooltip")
else:
    bad("card renders drift badge")
if "Roblox actualizado" in html:
    ok("drift badge label is Spanish")
else:
    bad("drift badge label is Spanish")
bd = html.find("if (i.degraded)")
br = html.find("i.roblox_drift)")
bb = html.find("i.setup === 'booting'")
if 0 <= bd < br < bb:
    ok("badge priority: degraded > drift > setup states")
else:
    bad("badge priority", f"{bd} {br} {bb}")
m = re.search(r"if \(instances\.some\(i =>(.*?)\)\) \{\s*\n?\s*setTimeout\(refresh, 5000\)",
              html, re.S)
if m and "roblox_drift" not in m.group(1):
    ok("drift does not force auto-refresh (human-action state)")
else:
    bad("drift in auto-refresh condition")

# ------------------------------------------------- E. regressions
print("== E. regressions ==")
for name, path, expect in (("watchdog-v6", V6_TEST, "PASS=40 FAIL=0"),
                           ("instance-health", IH_TEST, "17 passed, 0 failed")):
    r = subprocess.run(["bash" if path.endswith(".sh") else "python3", path],
                       capture_output=True, text=True)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    if r.returncode == 0 and any(expect in line for line in tail):
        ok(f"{name} suite still green")
    else:
        bad(f"{name} suite still green",
            f"rc={r.returncode} tail={' / '.join(tail)}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
