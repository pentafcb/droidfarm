#!/usr/bin/env python3
"""Unit tests for login-script-600 (manager/app.py only).

REAL GAP FOUND by auditing the atomic-write idiom after the
state-600-complete session closed the state-file 600 audit: the one
credential-adjacent manager write still unhardened was the Roblox
login script. _roblox_login_inner wrote auto_login.sh with a bare
open("w") — no tmp+rename, no chmod — into the instance's shared
media dir (/data/media/0/jerusalem/), readable from inside Android
by any app while the login ran, and until the startup sweep after a
manager crash mid-login. The script carries the account username
(shlex-quoted) and the login tap flow; the pwd.txt file next to it
already had the tmp+chmod+replace treatment, the script did not.
Its crash leftover had a second hole: sweep_stale_pwd_files removed
auto_login.sh but NOT auto_login.sh.tmp, so a crash between the tmp
write and the rename (once the script goes atomic) would leave the
username behind in a new file the sweep never touched.

Fix: the script is written to auto_login.sh.tmp, chmodded to 600
BEFORE the replace (os.replace preserves the tmp's mode, so the
live file is 600 from birth; chmod OSError-guarded so it never
breaks a login), and the write-failure path removes the tmp as
well as pwd.txt and the script. The startup sweep now removes
auto_login.sh.tmp alongside the other three staging files.

Hermetic: app.py loaded via importlib with JERUSALEM_DATA pointed
at a tmp dir and VOLUMES_BASE rebound to a tmp volume tree; docker,
waydroid, and the network are all faked (waydroid_exec stub reads
the staged script and records its mode at exec time). umask is set
to 022 in-process so a missing chmod would show up as mode 644.

Run: python3 test_login_script_600.py
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

BASE = tempfile.mkdtemp(prefix="ls600_")
DATA = os.path.join(BASE, "data")
VOL = os.path.join(BASE, "vol")
os.makedirs(DATA)
os.makedirs(VOL)
os.environ["JERUSALEM_DATA"] = DATA
os.umask(0o022)


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("ls600_app", os.path.join(REPO, "manager", "app.py"))
app.VOLUMES_BASE = VOL
with open(os.path.join(REPO, "manager", "app.py")) as f:
    APP_SRC = f.read()

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL:", name)


def jer(name):
    return os.path.join(VOL, "wd-%s-data" % name, "_data", "data",
                        "media", "0", "jerusalem")


def seed(name, username, password, provider="local"):
    with open(os.path.join(DATA, "instances.json"), "w") as f:
        json.dump({name: {"provider": provider}}, f)
    with open(os.path.join(DATA, "roblox_accounts.json"), "w") as f:
        json.dump({name: {"username": username, "password": password}}, f)
    os.makedirs(jer(name), exist_ok=True)


def run_login(name):
    """Call _roblox_login_inner with docker/network faked. Returns
    (ok, msg, captured) where captured holds what the fake
    waydroid_exec saw at exec time."""
    captured = {}
    real_exec = app.waydroid_exec

    def fake_exec(nm, cmd, timeout=None):
        sp = os.path.join(jer(nm), "auto_login.sh")
        pp = os.path.join(jer(nm), "pwd.txt")
        if os.path.exists(sp):
            captured["script_mode"] = stat.S_IMODE(os.stat(sp).st_mode)
            with open(sp) as f:
                captured["script"] = f.read()
        if os.path.exists(pp):
            captured["pwd_mode"] = stat.S_IMODE(os.stat(pp).st_mode)
        captured["cmd"] = cmd
        return 0, ""

    with mock.patch.object(app, "_send_input_cmds", lambda *a, **k: None), \
            mock.patch.object(app, "launch_roblox", lambda *a, **k: None), \
            mock.patch.object(app, "waydroid_exec", fake_exec), \
            mock.patch.object(app.time, "sleep", lambda s: None):
        ok, msg = app._roblox_login_inner(name)
    return ok, msg, captured


# --- 1. Happy path: script 600 at exec time, gone afterwards ---
seed("rbx1", "testuser", "s3cret-pw")
ok, msg, cap = run_login("rbx1")
check("happy path ok", ok is True)
check("exec ran the login script",
      cap.get("cmd", "").endswith("auto_login.sh 2>&1"))
check("script mode 600 at exec time (not umask 644)",
      cap.get("script_mode") == 0o600)
check("pwd mode 600 at exec time", cap.get("pwd_mode") == 0o600)
check("script holds username", "testuser" in cap.get("script", ""))
check("script holds verified tap coords",
      "input tap 512 228" in cap.get("script", "") and
      "input tap 512 367" in cap.get("script", ""))
check("script never carries the password",
      "s3cret-pw" not in cap.get("script", ""))
check("no script tmp left after login",
      not os.path.exists(os.path.join(jer("rbx1"), "auto_login.sh.tmp")))
check("script removed after login",
      not os.path.exists(os.path.join(jer("rbx1"), "auto_login.sh")))
check("pwd removed after login",
      not os.path.exists(os.path.join(jer("rbx1"), "pwd.txt")))

# --- 2. Hostile username stays inert in the staged script ---
seed("rbx1", "a b$(touch /tmp/pwn)", "pw2")
ok, msg, cap = run_login("rbx1")
check("hostile username login ok", ok is True)
check("space %s-encoded", "%s" in cap.get("script", ""))
check("hostile username fully single-quoted (inert)",
      "input text 'a%sb$(touch%s/tmp/pwn)'" in cap.get("script", ""))
check("script still 600 with hostile username",
      cap.get("script_mode") == 0o600)

# --- 3. Script-write failure: tmp dir blocks the write ---
seed("rbx1", "testuser", "pw3")
os.makedirs(os.path.join(jer("rbx1"), "auto_login.sh.tmp"), exist_ok=True)
ok, msg, cap = run_login("rbx1")
check("script-write failure returns False", ok is False)
check("script-write failure message",
      msg == "could not write login script")
check("pwd discarded on script-write failure",
      not os.path.exists(os.path.join(jer("rbx1"), "pwd.txt")))
check("no live script on script-write failure",
      not os.path.exists(os.path.join(jer("rbx1"), "auto_login.sh")))
os.rmdir(os.path.join(jer("rbx1"), "auto_login.sh.tmp"))

# --- 4. No credentials: unchanged refusal, nothing staged ---
seed("rbx1", "", "")
ok, msg, cap = run_login("rbx1")
check("no-credentials refusal", ok is False and
      msg == "no credentials saved for this instance")
check("nothing staged on refusal",
      not os.path.exists(os.path.join(jer("rbx1"), "auto_login.sh")) and
      not os.path.exists(os.path.join(jer("rbx1"), "pwd.txt")))

# --- 5. Startup sweep removes the script tmp too ---
seed("rbx1", "u", "p")
for fn in ("pwd.txt", "pwd.txt.tmp", "auto_login.sh", "auto_login.sh.tmp",
           "cmd-abc123.sh", "keep.txt"):
    with open(os.path.join(jer("rbx1"), fn), "w") as f:
        f.write("x")
seed_remote = os.path.join(VOL, "wd-rbx9-data", "_data", "data",
                           "media", "0", "jerusalem")
os.makedirs(seed_remote, exist_ok=True)
with open(os.path.join(seed_remote, "auto_login.sh.tmp"), "w") as f:
    f.write("remote")
n = app.sweep_stale_pwd_files(
    instances={"rbx1": {"provider": "local"},
               "rbx9": {"provider": "wing1"},
               "bad name!": {"provider": "local"}}, volbase=VOL)
check("sweep removes all 5 local staging files", n == 5)
check("sweep removed script tmp",
      not os.path.exists(os.path.join(jer("rbx1"), "auto_login.sh.tmp")))
check("sweep removed live script",
      not os.path.exists(os.path.join(jer("rbx1"), "auto_login.sh")))
check("sweep keeps unrelated file",
      os.path.exists(os.path.join(jer("rbx1"), "keep.txt")))
check("sweep skips remote instances",
      os.path.exists(os.path.join(seed_remote, "auto_login.sh.tmp")))
check("sweep never raises on missing dir",
      app.sweep_stale_pwd_files(
          instances={"ghost": {"provider": "local"}},
          volbase=os.path.join(BASE, "nope")) == 0)

# --- 6. Source contracts ---
i_chmod = APP_SRC.find("os.chmod(script_tmp, 0o600)")
i_replace = APP_SRC.find("os.replace(script_tmp, script_path)")
check("chmod-before-replace order in source",
      0 < i_chmod < i_replace)
check("no bare open(script_path, \"w\") left",
      'open(script_path, "w")' not in APP_SRC)
check("script written via tmp name",
      'open(script_tmp, "w")' in APP_SRC)
check("sweep lists script tmp",
      'jer + "/auto_login.sh.tmp"' in APP_SRC)
check("write-failure path removes tmp",
      "os.remove(script_tmp)" in APP_SRC)

# --- 7. Regression proof: pre-fix commit lacks the fix ---
# Pinned to the pre-fix commit (not HEAD: HEAD moves to the fixed
# commit the moment this change is committed, which would make a
# HEAD-based check fail on every later run).
old = subprocess.run(["git", "-C", REPO, "show",
                      "0f93e5a:manager/app.py"],
                     capture_output=True, text=True).stdout
check("pre-fix code had bare script open",
      'open(script_path, "w")' in old)
check("pre-fix sweep lacked script tmp",
      'auto_login.sh.tmp' not in old)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
