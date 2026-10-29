"""Unit tests for waydroid-exec-cleanup (manager/app.py).

Run: python3 test_waydroid_exec_cleanup.py

REAL GAP FOUND by auditing the local login/volume path after the
login-script-cleanup session: that session closed the pwd.txt and
auto_login.sh halves, but waydroid_exec() — the workhorse behind every
local Android command (boot checks, package checks, the login script
exec, the do_instance_input FIFO fallback) — wrote its script to a
SHARED cmd.sh in the instance volume on every call and NEVER removed
it. It sat there forever holding the most recent command: viewer-typed
text via the do_instance_input fallback (whatever the user typed into
a viewer, including typed passwords), the login script invocation, and
every other android_cmd. Readable from inside Android and from the host
volume path, world-readable by default — the same leak class as
pwd.txt/auto_login.sh, and the last staging file in the local path
with no cleanup anywhere. No other code reads cmd.sh (grep proves it:
only waydroid_exec writes and executes it). A second latent bug rode
along: two concurrent waydroid_exec calls on one instance clobbered
each other's shared script, so one call could execute the other's
command.

Fix (manager/app.py only, no agent change, no version cascade):
  * waydroid_exec() stages a UNIQUE per-call script (cmd-<hex>.sh via
    secrets.token_hex) instead of the shared cmd.sh, executes it, and
    removes it afterwards on EVERY path: happy path, setup failure
    (partial write removed), exec failure / timeout (finally block).
    A manager crash mid-call is the one path the post-run rm cannot
    cover — sweep_stale_pwd_files() (run by main() at startup, before
    any thread starts) now also removes stale cmd.sh / cmd-*.sh files
    from local instance volumes, so the next restart cleans up.
    Remote instances are skipped by the sweep (their staging lives on
    the node); names are gated against traversal; the sweep listing is
    a flat dir scan with name-gated patterns only.
  * The legacy shared cmd.sh name is covered by the sweep too, so
    volumes written by pre-fix managers get cleaned on upgrade.

Hermetic: app.py loaded via importlib; VOLUMES_BASE monkeypatched to a
tmp dir; run/run_bytes faked (the fake simulates the container write
and rm so the file lifecycle is observable); load_accounts /
load_instances / _send_input_cmds / launch_roblox faked for the login
regression; time.sleep stubbed. No docker, no network, no real
DATA_DIR touched.
"""
import base64
import importlib.util
import os
import re
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

app = load("waydroid_exec_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
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

BASE = tempfile.mkdtemp(prefix="waydroid_exec_")
VOL = os.path.join(BASE, "volumes")
os.makedirs(VOL)
app.VOLUMES_BASE = VOL
ORIG_RUN = app.run
ORIG_RUN_BYTES = app.run_bytes
ORIG_LOAD_INSTANCES = app.load_instances

def jer(name):
    d = os.path.join(VOL, f"wd-{name}-data", "_data", "data", "media", "0", "jerusalem")
    os.makedirs(d, exist_ok=True)
    return d

def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)

FNAME_RE = re.compile(r"cmd-[0-9a-f]+\.sh")

class FakeShell:
    """Simulates the container side: setup writes the staged script
    into the volume dir, rm deletes it, exec leaves it alone."""
    def __init__(self, setup_rc=0, exec_rc=0, exec_out=b"ok", raise_exec=None):
        self.calls = []          # ("run"|"bytes", cmd)
        self.setup_rc = setup_rc
        self.exec_rc = exec_rc
        self.exec_out = exec_out
        self.raise_exec = raise_exec
    def run(self, cmd, timeout=30):
        self.calls.append(("run", cmd))
        if "rm -f" in cmd:
            m = FNAME_RE.search(cmd)
            if m:
                try:
                    os.remove(os.path.join(self.dir, m.group(0)))
                except OSError:
                    pass
            return 0, "", ""
        m = FNAME_RE.search(cmd)
        if m and "open(" in cmd:
            write(os.path.join(self.dir, m.group(0)), b"staged command")
        return self.setup_rc, "", ""
    def run_bytes(self, cmd, timeout=30):
        self.calls.append(("bytes", cmd))
        if self.raise_exec is not None:
            raise self.raise_exec
        return self.exec_rc, self.exec_out

def install_fake(fake, name):
    fake.dir = jer(name)
    app.run = fake.run
    app.run_bytes = fake.run_bytes

def restore():
    app.run = ORIG_RUN
    app.run_bytes = ORIG_RUN_BYTES

# ---------- waydroid_exec: happy path ----------

fake = FakeShell()
install_fake(fake, "rbx1")
rc, out = app.waydroid_exec("rbx1", "getprop sys.boot_completed")
setup_cmd = fake.calls[0][1]
exec_cmd = fake.calls[1][1]
rm_cmd = fake.calls[2][1]
m = FNAME_RE.search(setup_cmd)
check("exec happy: returns rc 0 + decoded output", rc == 0 and out == "ok")
check("exec happy: setup writes a unique cmd-<hex>.sh, not shared cmd.sh",
      m is not None and "cmd.sh'" not in setup_cmd and "/cmd.sh" not in setup_cmd)
check("exec happy: exec runs the SAME unique name as the setup write",
      m is not None and m.group(0) in exec_cmd)
check("exec happy: rm issued after exec for that exact name",
      "rm -f" in rm_cmd and m is not None and m.group(0) in rm_cmd)
check("exec happy: staged file gone after the run (OLD CODE LEFT cmd.sh)",
      not os.path.exists(os.path.join(fake.dir, m.group(0))) if m else False)
check("exec happy: exactly setup + exec + rm, nothing else",
      len(fake.calls) == 3 and [c[0] for c in fake.calls] == ["run", "bytes", "run"])
restore()

# ---------- waydroid_exec: uniqueness ----------

fake = FakeShell()
install_fake(fake, "rbx2")
app.waydroid_exec("rbx2", "getprop a")
app.waydroid_exec("rbx2", "getprop b")
names = [FNAME_RE.search(c[1]).group(0) for c in fake.calls if "open(" in c[1]]
check("exec uniqueness: two calls stage two different filenames",
      len(names) == 2 and names[0] != names[1])
restore()

# ---------- waydroid_exec: failure paths ----------

fake = FakeShell(setup_rc=3)
install_fake(fake, "rbx3")
rc, out = app.waydroid_exec("rbx3", "getprop x")
check("exec setup failure: returns the setup rc, no exec attempted",
      rc == 3 and not any(c[0] == "bytes" for c in fake.calls))
check("exec setup failure: rm still attempted for the staged name",
      any("rm -f" in c[1] for c in fake.calls))
restore()

fake = FakeShell(exec_rc=1, exec_out=b"boom")
install_fake(fake, "rbx4")
rc, out = app.waydroid_exec("rbx4", "getprop x")
check("exec nonzero rc: rc + output preserved, rm still attempted",
      rc == 1 and out == "boom"
      and any("rm -f" in c[1] for c in fake.calls))
restore()

fake = FakeShell(raise_exec=RuntimeError("waydroid down"))
install_fake(fake, "rbx5")
try:
    app.waydroid_exec("rbx5", "getprop x")
    check("exec raising: exception propagates", False)
except RuntimeError:
    check("exec raising: exception propagates", True)
check("exec raising: rm still attempted via finally",
      any("rm -f" in c[1] for c in fake.calls))
restore()

fake = FakeShell()
install_fake(fake, "rbx6")
rc, out = app.waydroid_exec("../evil", "getprop x")
check("exec invalid name: (1, '') with zero shell calls",
      (rc, out) == (1, "") and fake.calls == [])
rc, out = app.waydroid_exec("rbx; rm -rf /", "getprop x")
check("exec metachar name: (1, '') with zero shell calls",
      (rc, out) == (1, "") and fake.calls == [])
restore()

# ---------- waydroid_exec: full login regression ----------

ORIG_SLEEP = time.sleep
time.sleep = lambda s: None
try:
    app.load_accounts = lambda: {"rbx9": {"username": "user1", "password": "P@ssw0rd!$"}}
    app.load_instances = lambda: {"rbx9": {"provider": "local"}}
    app._send_input_cmds = lambda n, cmds: True
    app.launch_roblox = lambda n: True
    fake = FakeShell()
    install_fake(fake, "rbx9")
    d = jer("rbx9")
    ok, msg = app._roblox_login_inner("rbx9")
    exec_cmds = [c[1] for c in fake.calls if c[0] == "bytes"]
    staged_cmds = [c[1] for c in fake.calls if "open(" in c[1]]
    # The auto_login.sh reference lives in the staged script CONTENT
    # (base64 in the setup command), not in the exec command line —
    # decode it the way the container does.
    staged_content = ""
    for c in staged_cmds:
        m = re.search(r"b64decode\('([^']+)'\)", c)
        if m:
            staged_content = base64.b64decode(m.group(1)).decode()
    check("login regression: still returns True", ok is True)
    check("login regression: staged script content runs auto_login.sh",
          "auto_login.sh" in staged_content)
    check("login regression: pwd.txt + auto_login.sh gone (no regression)",
          not os.path.exists(os.path.join(d, "pwd.txt"))
          and not os.path.exists(os.path.join(d, "auto_login.sh")))
    staged = [c[1] for c in fake.calls if "open(" in c[1]]
    check("login regression: login exec used a unique cmd-<hex>.sh",
          len(staged) == 1 and FNAME_RE.search(staged[0]) is not None)
    check("login regression: no cmd*.sh left in the volume after login",
          not any(f.startswith("cmd-") or f == "cmd.sh" for f in os.listdir(d)))
finally:
    time.sleep = ORIG_SLEEP
    app.load_instances = ORIG_LOAD_INSTANCES
    restore()

# ---------- sweep_stale_pwd_files: cmd.sh half ----------

d = jer("rbx1")
write(os.path.join(d, "cmd.sh"), b"getprop x")
check("sweep: legacy shared cmd.sh removed, count 1",
      app.sweep_stale_pwd_files(instances={"rbx1": {"provider": "local"}}, volbase=VOL) == 1
      and not os.path.exists(os.path.join(d, "cmd.sh")))

d = jer("rbx2")
write(os.path.join(d, "cmd-ab12cd34ef56ab78.sh"), b"input text secret")
check("sweep: per-call cmd-<hex>.sh removed, count 1",
      app.sweep_stale_pwd_files(instances={"rbx2": {"provider": "local"}}, volbase=VOL) == 1
      and not os.path.exists(os.path.join(d, "cmd-ab12cd34ef56ab78.sh")))

d = jer("rbx3")
write(os.path.join(d, "pwd.txt"), b"S")
write(os.path.join(d, "pwd.txt.tmp"), b"S")
write(os.path.join(d, "auto_login.sh"), b"input tap 512 228")
write(os.path.join(d, "cmd.sh"), b"getprop x")
write(os.path.join(d, "cmd-0011223344556677.sh"), b"input text x")
check("sweep: pwd + tmp + script + both cmd files removed, count 5",
      app.sweep_stale_pwd_files(instances={"rbx3": {"provider": "local"}}, volbase=VOL) == 5
      and not any(os.path.exists(os.path.join(d, f)) for f in
                  ("pwd.txt", "pwd.txt.tmp", "auto_login.sh", "cmd.sh",
                   "cmd-0011223344556677.sh")))

d = jer("rbx4")
for f in ("live.png", "booted", "input.fifo", "stream_wanted", "notes.txt",
          "cmd.txt", "xcmd-foo.sh", "cmd-", "mycmd.sh"):
    write(os.path.join(d, f), b"keep")
check("sweep: unrelated files untouched (incl. cmd.txt, xcmd-*.sh, cmd-)",
      app.sweep_stale_pwd_files(instances={"rbx4": {"provider": "local"}}, volbase=VOL) == 0
      and all(os.path.exists(os.path.join(d, f)) for f in
              ("live.png", "booted", "input.fifo", "stream_wanted", "notes.txt",
               "cmd.txt", "xcmd-foo.sh", "cmd-", "mycmd.sh")))

d = jer("rbx5")
os.makedirs(os.path.join(d, "cmd.sh"), exist_ok=True)  # a dir, not a file
check("sweep: cmd.sh as directory -> 0, no raise, dir untouched",
      app.sweep_stale_pwd_files(instances={"rbx5": {"provider": "local"}}, volbase=VOL) == 0
      and os.path.isdir(os.path.join(d, "cmd.sh")))
shutil.rmtree(os.path.join(d, "cmd.sh"), ignore_errors=True)

d = jer("srt1")
write(os.path.join(d, "cmd.sh"), b"getprop x")
check("sweep: remote instance skipped, cmd.sh untouched",
      app.sweep_stale_pwd_files(instances={"srt1": {"provider": "wing-1"}}, volbase=VOL) == 0
      and os.path.exists(os.path.join(d, "cmd.sh")))

evil = os.path.join(BASE, "evil-cmd.sh")
write(evil, b"S")
check("sweep: invalid name skipped, no traversal delete",
      app.sweep_stale_pwd_files(instances={"../evil": {"provider": "local"}}, volbase=VOL) == 0
      and os.path.exists(evil))

check("sweep: missing instance dir -> 0, no raise",
      app.sweep_stale_pwd_files(instances={"ghost": {"provider": "local"}}, volbase=VOL) == 0)

# ---------- source contracts ----------

exec_src = APP_SRC[APP_SRC.index("def waydroid_exec("):APP_SRC.index("def check_roblox(")]
check("source: no shared cmd.sh literal in waydroid_exec (write or exec)",
      "/cmd.sh" not in exec_src)
check("source: unique per-call name via secrets.token_hex",
      'fname = "cmd-%s.sh" % secrets.token_hex(8)' in exec_src)
check("source: post-run rm in a finally block (covers exec failure)",
      "finally:" in exec_src
      and "rm -f" in exec_src[exec_src.index("finally:"):])
check("source: setup-failure path also attempts the rm",
      exec_src.count("rm -f") >= 2)
sweep_src = APP_SRC[APP_SRC.index("def sweep_stale_pwd_files("):APP_SRC.index("def _roblox_login_inner(")]
check("source: sweep covers legacy cmd.sh + per-call cmd-*.sh",
      'fn == "cmd.sh"' in sweep_src
      and 'fn.startswith("cmd-")' in sweep_src
      and 'fn.endswith(".sh")' in sweep_src)
check("source: cmd.sh has no other readers (write + exec + rm + sweep only)",
      APP_SRC.count("cmd.sh") <= 6)

shutil.rmtree(BASE, ignore_errors=True)
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
