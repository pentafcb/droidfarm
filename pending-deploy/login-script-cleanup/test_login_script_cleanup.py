"""Unit tests for login script cleanup (manager/app.py).

Run: python3 test_login_script_cleanup.py

REAL BUG FIXED (follow-up to login-pwd-cleanup):
  _roblox_login_inner() staged the local Roblox password as pwd.txt and
  the login script as auto_login.sh in the instance volume. The
  login-pwd-cleanup session fixed the pwd.txt half (atomic 600 staging,
  discard on every exit path, startup sweep) but the script half was
  left behind: auto_login.sh was written on every local login and NEVER
  removed — not on any exit path, not by the startup sweep, not by
  anything else (it has no other reader anywhere in the codebase).
  The script carries the account username (shlex-quoted) plus the
  verified login tap coordinates, world-readable by default, readable
  from inside Android and from the host volume path, persisting
  forever after every login. Same leak class, smaller secret.

Fix (manager/app.py only, no agent change, no version cascade):
  * _discard_login_script() removes auto_login.sh, best-effort, never
    raises, and runs on EVERY exit path: happy path (finally), raising
    waydroid_exec (finally), and failed script write.
  * sweep_stale_pwd_files() (name kept from the pwd.txt-only era) now
    also removes auto_login.sh leftovers from local instance volumes;
    main() runs it at startup, so a manager crash between the staging
    writes and their removal is cleaned up on the next restart.
    Remote instances are skipped (their staging lives on the node);
    names are gated so a hand-edited instances.json can never turn
    the sweep into a path-traversal delete.

Hermetic: app.py loaded via importlib; VOLUMES_BASE monkeypatched to a
tmp dir; load_accounts / load_instances / _send_input_cmds /
launch_roblox / waydroid_exec faked; time.sleep stubbed. No docker, no
network, no real DATA_DIR touched.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod

app = load("loginscript_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
with open(os.path.join(HERE, "..", "..", "manager", "app.py")) as f:
    APP_SRC = f.read()

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")

BASE = tempfile.mkdtemp(prefix="loginscript_")
VOL = os.path.join(BASE, "volumes")
os.makedirs(VOL)
app.VOLUMES_BASE = VOL
ORIG_LOAD_INSTANCES = app.load_instances

def jer(name):
    d = os.path.join(VOL, f"wd-{name}-data", "_data", "data", "media", "0", "jerusalem")
    os.makedirs(d, exist_ok=True)
    return d

def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)

# ---------- sweep_stale_pwd_files (script half) ----------

d = jer("rbx1")
write(os.path.join(d, "auto_login.sh"), b"input tap 512 228")
check("sweep: local auto_login.sh removed, count 1",
      app.sweep_stale_pwd_files(instances={"rbx1": {"provider": "local"}}, volbase=VOL) == 1
      and not os.path.exists(os.path.join(d, "auto_login.sh")))

d = jer("rbx2")
write(os.path.join(d, "pwd.txt"), b"S")
write(os.path.join(d, "pwd.txt.tmp"), b"S")
write(os.path.join(d, "auto_login.sh"), b"input tap 512 228")
check("sweep: pwd.txt + tmp + script all removed, count 3",
      app.sweep_stale_pwd_files(instances={"rbx2": {"provider": "local"}}, volbase=VOL) == 3
      and not os.path.exists(os.path.join(d, "pwd.txt"))
      and not os.path.exists(os.path.join(d, "pwd.txt.tmp"))
      and not os.path.exists(os.path.join(d, "auto_login.sh")))

d = jer("srt1")
write(os.path.join(d, "auto_login.sh"), b"input tap 512 228")
check("sweep: remote instance skipped, script untouched",
      app.sweep_stale_pwd_files(instances={"srt1": {"provider": "wing-1"}}, volbase=VOL) == 0
      and os.path.exists(os.path.join(d, "auto_login.sh")))

evil = os.path.join(BASE, "evil-script.sh")
write(evil, b"S")
check("sweep: invalid name skipped, no traversal delete",
      app.sweep_stale_pwd_files(instances={"../evil": {"provider": "local"}}, volbase=VOL) == 0
      and os.path.exists(evil))

check("sweep: missing instance dir -> 0, no raise",
      app.sweep_stale_pwd_files(instances={"ghost": {"provider": "local"}}, volbase=VOL) == 0)

d = jer("rbx5")
os.makedirs(os.path.join(d, "auto_login.sh"), exist_ok=True)  # a dir, not a file
check("sweep: undeletable script (is a dir) -> 0, no raise",
      app.sweep_stale_pwd_files(instances={"rbx5": {"provider": "local"}}, volbase=VOL) == 0)

# ---------- _discard_login_script ----------

d = jer("rbx7")
write(os.path.join(d, "auto_login.sh"), b"input tap 512 228")
app._discard_login_script(os.path.join(d, "auto_login.sh"))
check("discard: removes auto_login.sh",
      not os.path.exists(os.path.join(d, "auto_login.sh")))
try:
    app._discard_login_script(os.path.join(d, "auto_login.sh"))
    check("discard: missing file -> no raise", True)
except Exception:
    check("discard: missing file -> no raise", False)
try:
    os.makedirs(os.path.join(d, "auto_login.sh"), exist_ok=True)
    app._discard_login_script(os.path.join(d, "auto_login.sh"))
    check("discard: is-a-directory -> no raise, dir untouched",
          os.path.isdir(os.path.join(d, "auto_login.sh")))
except Exception:
    check("discard: is-a-directory -> no raise, dir untouched", False)
shutil.rmtree(os.path.join(d, "auto_login.sh"), ignore_errors=True)

# ---------- _roblox_login_inner ----------

ORIG_SLEEP = time.sleep
time.sleep = lambda s: None

def fake_login_env():
    app.load_accounts = lambda: {"rbx9": {"username": "user1", "password": "P@ssw0rd!$"}}
    app.load_instances = lambda: {"rbx9": {"provider": "local"}}
    app._send_input_cmds = lambda n, cmds: True
    app.launch_roblox = lambda n: True

seen = {}
def fake_exec_ok(name, cmd, timeout=30):
    d = jer("rbx9")
    _sh = os.path.join(d, "auto_login.sh")
    seen["script_during_exec"] = open(_sh).read() if os.path.exists(_sh) else None
    seen["cmd"] = cmd
    return 0, ""

try:
    fake_login_env()
    app.waydroid_exec = fake_exec_ok
    d = jer("rbx9")
    ok, msg = app._roblox_login_inner("rbx9")
    check("login happy: returns True", ok is True)
    check("login happy: script present during exec with username line",
          seen.get("script_during_exec") is not None
          and "input text user1" in seen["script_during_exec"])
    check("login happy: exec runs the auto_login.sh script",
          "auto_login.sh" in (seen.get("cmd") or ""))
    check("login happy: auto_login.sh gone after exec (OLD CODE LEFT IT)",
          not os.path.exists(os.path.join(d, "auto_login.sh")))
    check("login happy: pwd.txt still gone after exec (no regression)",
          not os.path.exists(os.path.join(d, "pwd.txt")))

    def fake_exec_raise(name, cmd, timeout=30):
        raise RuntimeError("waydroid down")
    fake_login_env()
    app.waydroid_exec = fake_exec_raise
    d = jer("rbx9")
    try:
        app._roblox_login_inner("rbx9")
        check("login raising exec: exception propagates", False)
    except RuntimeError:
        check("login raising exec: exception propagates", True)
    check("login raising exec: script gone via finally (regression proof)",
          not os.path.exists(os.path.join(d, "auto_login.sh")))
    check("login raising exec: pwd.txt gone via finally (no regression)",
          not os.path.exists(os.path.join(d, "pwd.txt")))

    # script-write failure: auto_login.sh is a directory -> open() fails
    fake_login_env()
    app.waydroid_exec = fake_exec_ok
    d = jer("rbx9")
    _sh = os.path.join(d, "auto_login.sh")
    if os.path.isfile(_sh):
        os.remove(_sh)
    os.makedirs(_sh, exist_ok=True)
    ok, msg = app._roblox_login_inner("rbx9")
    check("login script-write failure: returns False", ok is False and "could not write login script" in msg)
    check("login script-write failure: pwd.txt gone (no regression)",
          not os.path.exists(os.path.join(d, "pwd.txt")))
    shutil.rmtree(_sh, ignore_errors=True)
finally:
    time.sleep = ORIG_SLEEP
    app.load_instances = ORIG_LOAD_INSTANCES

# ---------- source contracts ----------

inner_src = APP_SRC[APP_SRC.index("def _roblox_login_inner"):]
inner_src = inner_src[:inner_src.index("def roblox_login(")]
check("source: _discard_login_script defined, never raises (OSError catch)",
      "def _discard_login_script(script_path):" in APP_SRC
      and "except OSError:" in APP_SRC[APP_SRC.index("def _discard_login_script"):])
check("source: finally discards both pwd file and script",
      "_discard_pwd_file(pwd_path)" in inner_src
      and "_discard_login_script(script_path)" in inner_src)
check("source: script-write failure path discards the script too",
      inner_src.count("_discard_login_script(script_path)") >= 2)
check("source: sweep removes auto_login.sh alongside pwd files",
      'jer + "/auto_login.sh"' in APP_SRC[APP_SRC.index("def sweep_stale_pwd_files"):APP_SRC.index("def _roblox_login_inner")])
check("source: main() startup sweep still wired (covers crash leftovers)",
      "sweep_stale_pwd_files()" in APP_SRC[APP_SRC.index("def main():"):])
check("source: script has no other readers (write + exec + discard + sweep only)",
      APP_SRC.count("auto_login.sh") <= 8)

shutil.rmtree(BASE, ignore_errors=True)
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
