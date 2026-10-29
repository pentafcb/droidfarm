"""Unit tests for login password cleanup (manager/app.py).

Run: python3 test_login_pwd_cleanup.py

REAL BUG FIXED:
  _roblox_login_inner() staged the local Roblox password as a cleartext
  pwd.txt in the instance volume (bare open("wb"), default perms) and
  removed it ONLY on the happy path. Two leak paths:
  (1) the auto_login.sh write failed -> the function returned
      "could not write login script" WITHOUT removing pwd.txt;
  (2) a manager crash (kill -9, power loss) between the pwd.txt write
      and its removal left the cleartext password in the volume FOREVER
      - no later code ever cleaned it up, and the file is readable from
      the host volume path and from inside Android.
  The remote path already had a 60s delayed delete for its staged
  input-b64.txt; the local path had nothing.

Fix (manager/app.py only, no agent change, no version cascade):
  * pwd.txt is written atomically (tmp+rename) with 600 perms.
  * _discard_pwd_file() removes pwd.txt + pwd.txt.tmp, best-effort,
    never raises, and runs on EVERY exit path: happy path (finally),
    raising waydroid_exec (finally), and failed script write.
  * sweep_stale_pwd_files() removes leftovers from local instance
    volumes; main() runs it at startup, before any login thread can
    start, so a manager restart (the recovery path from exactly the
    crash in (2)) always cleans up. Remote instances are skipped
    (their staging lives on the node); names are gated so a
    hand-edited instances.json can never turn the sweep into a
    path-traversal delete.

Hermetic: app.py loaded via importlib; VOLUMES_BASE monkeypatched to a
tmp dir; load_accounts / load_instances / _send_input_cmds /
launch_roblox / waydroid_exec faked; time.sleep stubbed (the login
path sleeps 10s waiting for the Roblox login screen). No docker, no
network, no real DATA_DIR touched.
"""
import importlib.util
import os
import shutil
import stat
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

app = load("loginpwd_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
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

BASE = tempfile.mkdtemp(prefix="loginpwd_")
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

# ---------- sweep_stale_pwd_files ----------

d = jer("rbx1")
write(os.path.join(d, "pwd.txt"), b"SECRET")
check("sweep: local pwd.txt removed, count 1",
      app.sweep_stale_pwd_files(instances={"rbx1": {"provider": "local"}}, volbase=VOL) == 1
      and not os.path.exists(os.path.join(d, "pwd.txt")))

d = jer("rbx2")
write(os.path.join(d, "pwd.txt"), b"S")
write(os.path.join(d, "pwd.txt.tmp"), b"S")
check("sweep: pwd.txt + pwd.txt.tmp both removed, count 2",
      app.sweep_stale_pwd_files(instances={"rbx2": {"provider": "local"}}, volbase=VOL) == 2
      and not os.path.exists(os.path.join(d, "pwd.txt"))
      and not os.path.exists(os.path.join(d, "pwd.txt.tmp")))

d = jer("srt1")
write(os.path.join(d, "pwd.txt"), b"S")
check("sweep: remote instance skipped, file untouched",
      app.sweep_stale_pwd_files(instances={"srt1": {"provider": "wing-1"}}, volbase=VOL) == 0
      and os.path.exists(os.path.join(d, "pwd.txt")))

d = jer("rbx3")
write(os.path.join(d, "pwd.txt"), b"S")
check("sweep: missing provider treated as local (matches login default)",
      app.sweep_stale_pwd_files(instances={"rbx3": {}}, volbase=VOL) == 1
      and not os.path.exists(os.path.join(d, "pwd.txt")))

d = jer("rbx4")
write(os.path.join(d, "pwd.txt"), b"S")
check("sweep: non-dict instance info tolerated, treated as local",
      app.sweep_stale_pwd_files(instances={"rbx4": "garbage"}, volbase=VOL) == 1
      and not os.path.exists(os.path.join(d, "pwd.txt")))

evil = os.path.join(BASE, "evil-pwd.txt")
write(evil, b"S")
check("sweep: invalid name skipped, no traversal delete",
      app.sweep_stale_pwd_files(instances={"../evil": {"provider": "local"}}, volbase=VOL) == 0
      and os.path.exists(evil))

check("sweep: missing instance dir -> 0, no raise",
      app.sweep_stale_pwd_files(instances={"ghost": {"provider": "local"}}, volbase=VOL) == 0)

check("sweep: non-dict instances -> 0, no raise",
      app.sweep_stale_pwd_files(instances=["rbx1"], volbase=VOL) == 0)

d = jer("rbx5")
os.makedirs(os.path.join(d, "pwd.txt"), exist_ok=True)  # a dir, not a file
check("sweep: undeletable pwd.txt (is a dir) -> 0, no raise",
      app.sweep_stale_pwd_files(instances={"rbx5": {"provider": "local"}}, volbase=VOL) == 0)

app.load_instances = lambda: {"rbx6": {"provider": "local"}}
d = jer("rbx6")
write(os.path.join(d, "pwd.txt"), b"S")
try:
    check("sweep: instances=None loads from load_instances()",
          app.sweep_stale_pwd_files(volbase=VOL) == 1
          and not os.path.exists(os.path.join(d, "pwd.txt")))
finally:
    app.load_instances = ORIG_LOAD_INSTANCES

app.load_instances = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    check("sweep: load_instances raising -> 0, never raises", app.sweep_stale_pwd_files(volbase=VOL) == 0)
finally:
    app.load_instances = ORIG_LOAD_INSTANCES

d = jer("rbx8")
write(os.path.join(d, "pwd.txt"), b"S")
check("sweep: volbase omitted -> uses VOLUMES_BASE (already monkeypatched)",
      app.sweep_stale_pwd_files(instances={"rbx8": {"provider": "local"}}) == 1
      and not os.path.exists(os.path.join(d, "pwd.txt")))

# ---------- _discard_pwd_file ----------
d = jer("rbx7")
write(os.path.join(d, "pwd.txt"), b"S")
write(os.path.join(d, "pwd.txt.tmp"), b"S")
app._discard_pwd_file(os.path.join(d, "pwd.txt"))
check("discard: removes pwd.txt + tmp",
      not os.path.exists(os.path.join(d, "pwd.txt")) and not os.path.exists(os.path.join(d, "pwd.txt.tmp")))
try:
    app._discard_pwd_file(os.path.join(d, "pwd.txt"))
    check("discard: missing files -> no raise", True)
except Exception:
    check("discard: missing files -> no raise", False)

# ---------- _roblox_login_inner ----------
ORIG_SLEEP = time.sleep
time.sleep = lambda s: None

def fake_login_env(pwd="P@ssw0rd!$"):
    app.load_accounts = lambda: {"rbx9": {"username": "user1", "password": pwd}}
    app.load_instances = lambda: {"rbx9": {"provider": "local"}}
    app._send_input_cmds = lambda n, cmds: True
    app.launch_roblox = lambda n: True

seen = {}
def fake_exec_ok(name, cmd, timeout=30):
    d = jer("rbx9")
    p = os.path.join(d, "pwd.txt")
    seen["exists_during_exec"] = os.path.exists(p)
    seen["mode_during_exec"] = oct(stat.S_IMODE(os.stat(p).st_mode)) if os.path.exists(p) else None
    seen["content_during_exec"] = open(p, "rb").read() if os.path.exists(p) else None
    # login-script-cleanup: the script is discarded in the finally after
    # exec, so capture it here (during exec) instead of after the login.
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
    check("login happy: pwd.txt present during exec with exact password",
          seen.get("exists_during_exec") is True and seen.get("content_during_exec") == b"P@ssw0rd!$")
    check("login happy: pwd.txt staged with 600 perms", seen.get("mode_during_exec") == "0o600")
    check("login happy: exec runs the auto_login.sh script", "auto_login.sh" in (seen.get("cmd") or ""))
    check("login happy: pwd.txt gone after exec", not os.path.exists(os.path.join(d, "pwd.txt")))
    check("login happy: pwd.txt.tmp gone after exec", not os.path.exists(os.path.join(d, "pwd.txt.tmp")))
    check("login happy: auto_login.sh types password via pwd.txt cat",
          seen.get("script_during_exec") is not None and
          'cat /data/media/0/jerusalem/pwd.txt' in seen["script_during_exec"])

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
    check("login raising exec: pwd.txt gone via finally (regression proof)",
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
    check("login script-write failure: pwd.txt gone (OLD CODE LEAKED IT)",
          not os.path.exists(os.path.join(d, "pwd.txt")))
    shutil.rmtree(os.path.join(d, "auto_login.sh"))

    # pwd-write failure: jerusalem path is a file, not a dir
    fake_login_env()
    app.waydroid_exec = fake_exec_ok
    d = jer("rbx9")
    os.remove(os.path.join(d, "auto_login.sh")) if os.path.exists(os.path.join(d, "auto_login.sh")) else None
    # replace the jer dir with a file: remove dir tree, write file in its place
    shutil.rmtree(d)
    with open(d, "wb") as f:
        f.write(b"not a dir")
    ok, msg = app._roblox_login_inner("rbx9")
    check("login pwd-write failure: returns False, never raises",
          ok is False and "could not write password file" in msg)
finally:
    time.sleep = ORIG_SLEEP
    app.load_instances = ORIG_LOAD_INSTANCES

# ---------- source contracts ----------
main_src = APP_SRC[APP_SRC.index("def main():"):]
check("source: main() runs the startup sweep",
      "sweep_stale_pwd_files()" in main_src)
check("source: sweep runs before thread registrations in main()",
      main_src.index("sweep_stale_pwd_files()") < main_src.index("register_supervised"))
check("source: no bare pwd.txt write remains",
      'open(f"{vol_path}/pwd.txt", "wb")' not in APP_SRC)
check("source: pwd staged via tmp+rename with 600",
      'os.chmod(pwd_tmp, 0o600)' in APP_SRC and 'os.replace(pwd_tmp, pwd_path)' in APP_SRC)
check("source: finally discards the password file",
      "finally:\n" in APP_SRC and "_discard_pwd_file(pwd_path)" in APP_SRC)
check("source: sweep gates names (traversal guard)",
      "valid_instance_name(name)" in APP_SRC[APP_SRC.index("def sweep_stale_pwd_files"):APP_SRC.index("def _roblox_login_inner")])

shutil.rmtree(BASE, ignore_errors=True)
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
