#!/usr/bin/env python3
"""Tests for instance-health surfacing (watchdog DEGRADED -> manager API -> dashboard).

Covers:
  A. manager.instance_health() — degraded marker parsing (fake DF_ST dir)
  B. /api/instances contract — both branches emit degraded/degraded_reason
  C. dashboard contract — index.html renders the degraded badge
  D. watchdog cb_guard writes the reason into the .degraded marker (real
     functions extracted from waydroid-image/df-watchdog.sh)
  E. regression — the existing watchdog-v4 suite still passes
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
WD_TEST = os.path.join(REPO, "pending-deploy", "watchdog-v4", "test_watchdog_v4.sh")

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
    spec = importlib.util.spec_from_file_location("df_app", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


print("== A. instance_health() ==")
with tempfile.TemporaryDirectory() as st:
    os.environ["DF_ST"] = st
    try:
        app = load_app()
    except Exception as e:  # noqa: BLE001
        bad("import manager/app.py", str(e))
        app = None
    if app is not None:
        h = app.instance_health("rbx1")
        if h == {"degraded": False, "reason": None, "drift": False, "drift_reason": None}:
            ok("no marker -> not degraded")
        else:
            bad("no marker -> not degraded", repr(h))

        open(os.path.join(st, "rbx1.degraded"), "w").close()
        h = app.instance_health("rbx1")
        if h["degraded"] is True and h["reason"] == "auto-recovery paused by watchdog":
            ok("empty marker -> degraded + generic reason")
        else:
            bad("empty marker -> degraded + generic reason", repr(h))

        with open(os.path.join(st, "rbx2.degraded"), "w") as f:
            f.write("container restarted 7 times in last 3600s - auto-recovery paused\n")
        h = app.instance_health("rbx2")
        if h["degraded"] is True and h["reason"] == \
                "container restarted 7 times in last 3600s - auto-recovery paused":
            ok("reason marker -> degraded + exact reason")
        else:
            bad("reason marker -> degraded + exact reason", repr(h))

        with open(os.path.join(st, "rbx3.degraded"), "w") as f:
            f.write("first line\nsecond line\n")
        h = app.instance_health("rbx3")
        if h["degraded"] is True and h["reason"] == "first line":
            ok("multiline marker -> first line only")
        else:
            bad("multiline marker -> first line only", repr(h))

        with open(os.path.join(st, "rbx4.degraded"), "w") as f:
            f.write("x" * 500 + "\n")
        h = app.instance_health("rbx4")
        if h["degraded"] is True and len(h["reason"]) == 200:
            ok("long reason truncated to 200 chars")
        else:
            bad("long reason truncated to 200 chars", repr(h))

        os.mkdir(os.path.join(st, "rbx5.degraded"))
        h = app.instance_health("rbx5")
        if h == {"degraded": False, "reason": None, "drift": False, "drift_reason": None}:
            ok("degraded path is a directory -> not degraded")
        else:
            bad("degraded path is a directory -> not degraded", repr(h))

        h = app.instance_health("../../etc/passwd")
        if h == {"degraded": False, "reason": None, "drift": False, "drift_reason": None}:
            ok("path traversal in name sanitized")
        else:
            bad("path traversal in name sanitized", repr(h))

    os.environ["DF_ST"] = os.path.join(st, "does-not-exist")
    try:
        h = app.instance_health("rbx1")
        if h == {"degraded": False, "reason": None, "drift": False, "drift_reason": None}:
            ok("missing state dir -> not degraded, no raise")
        else:
            bad("missing state dir -> not degraded, no raise", repr(h))
    except Exception as e:  # noqa: BLE001
        bad("missing state dir raised", str(e))

print("== B. /api/instances contract ==")
src = open(APP).read()
# local branch: the dict built after `health = instance_health(name)`
if re.search(r'"degraded": health\["degraded"\]', src) and \
        re.search(r'"degraded_reason": health\["reason"\]', src):
    ok("local branch emits degraded + degraded_reason from instance_health()")
else:
    bad("local branch emits degraded + degraded_reason from instance_health()")
# remote branch: filled from the node agent's selfwatch (>=1.0.22 reports
# degraded/_reason from its own per-instance markers; older agents omit the
# fields -> False/None, same as "no data") — see remote-degraded package.
m = re.search(r'"degraded": clean_agent_degraded\(rs\.get\("degraded"\)\)', src)
if m:
    ok("remote branch emits degraded from agent selfwatch (>=1.0.22)")
else:
    bad("remote branch emits degraded from agent selfwatch (>=1.0.22)")

print("== C. dashboard contract ==")
html = open(HTML).read()
if ".setup.degraded" in html and "var(--bad)" in html:
    ok("degraded CSS rule present")
else:
    bad("degraded CSS rule present")
if "i.degraded" in html and "setup degraded" in html and "i.degraded_reason" in html \
        and "ri-alert-line" in html:
    ok("card renders degraded badge with reason tooltip")
else:
    bad("card renders degraded badge with reason tooltip")
if re.search(r"i\.degraded\)\) \{\s*\n?\s*setTimeout\(refresh, 5000\)", html) or \
        "i.setup==='installing_roblox' || i.degraded" in html:
    ok("auto-refresh continues while any instance degraded")
else:
    bad("auto-refresh continues while any instance degraded")
# degraded checked before the setup states (highest-priority badge)
bi = html.find("if (i.degraded)")
bb = html.find("i.setup === 'booting'")
if 0 <= bi < bb:
    ok("degraded badge takes priority over setup badge")
else:
    bad("degraded badge takes priority over setup badge")

print("== D. watchdog cb_guard writes reason to marker ==")
with tempfile.TemporaryDirectory() as t:
    st, lg = os.path.join(t, "st"), os.path.join(t, "log")
    os.makedirs(st)
    driver = f"""
export DF_ST="{st}" DF_LOG="{lg}"
ST="{st}"; LOG="{lg}"
eval "$(sed -n '/^log() {{/,/^check_one() {{/p' "{WD}" | sed '$d')"
n=$(cb_guard "rbx9" "rbx9.restart" 3600 2 "container"); [ "$n" = "1" ] || exit 11
n=$(cb_guard "rbx9" "rbx9.restart" 3600 2 "container"); [ "$n" = "2" ] || exit 12
cb_guard "rbx9" "rbx9.restart" 3600 2 "container" >/dev/null || rc=$?
[ "$rc" = "1" ] || exit 13
[ -f "$ST/rbx9.degraded" ] || exit 14
grep -q "container restarted 3 times in last 3600s - auto-recovery paused" "$ST/rbx9.degraded" || exit 15
ev_before=$(wc -l < "$ST/rbx9.restart.ev")
dg_before=$(grep -c "DEGRADED" "$LOG")
cb_guard "rbx9" "rbx9.restart" 3600 2 "container" >/dev/null 2>&1 || rc2=$?
[ "$rc2" = "1" ] || exit 16
ev_after=$(wc -l < "$ST/rbx9.restart.ev"); [ "$ev_before" = "$ev_after" ] || exit 17
dg_after=$(grep -c "DEGRADED" "$LOG"); [ "$dg_before" = "$dg_after" ] || exit 18
echo DRIVER_OK
"""
    r = subprocess.run(["bash", "-c", driver], capture_output=True, text=True, timeout=60)
    if r.returncode == 0 and "DRIVER_OK" in r.stdout:
        ok("over-budget trip writes reason into .degraded marker")
        ok("repeat call while degraded: no new event, no repeat log spam")
    else:
        bad("watchdog cb_guard driver", f"rc={r.returncode} out={r.stdout[-300:]} err={r.stderr[-300:]}")

print("== E. watchdog-v4 suite regression ==")
r = subprocess.run(["bash", WD_TEST], capture_output=True, text=True, timeout=300)
tail = r.stdout.strip().splitlines()[-3:] if r.stdout.strip() else []
if r.returncode == 0:
    ok(f"watchdog-v4 suite still green ({' / '.join(tail)})")
else:
    bad("watchdog-v4 suite still green", f"rc={r.returncode} tail={' / '.join(tail)}")

print(f"\n{ PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
