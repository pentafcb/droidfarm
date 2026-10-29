#!/usr/bin/env python3
"""login-username-escape: saved Roblox username must not reach a shell unescaped.

Gap: _roblox_login_inner() (manager/app.py) interpolated the saved username
into auto_login.sh with only spaces->%s and quote-stripping:
    safe_user = username.replace(" ", "%s").replace("'", "").replace('"', "")
The script runs under `sh` as root inside the Android container, so a saved
username containing $(...), backticks, ;, |, &, newlines etc. executed
arbitrary commands in the container whenever a login sequence ran (Login
button, Login-all batch, boot auto-login watcher). Usernames enter verbatim
via the dashboard Account panel and the bulk import (only length-truncated).
The /input route's text allowlist and the wing agent's 1.0.2
sanitize_input_text already covered their sinks; this local login script was
the remaining unsanitized producer (the in-container input-daemon.sh runs
`eval "$cmd"` on FIFO lines, so quoting must be airtight).

Fix: shlex.quote(username.replace(" ", "%s")) at the sink — the %s space
encoding `input text` needs is preserved, everything else is inert under
POSIX sh. Password path unchanged (already double-quoted "$P").

Hermetic: real app import, faked accounts/instances/waydroid_exec/sleep;
the generated `input text` line is executed by a REAL sh with stubbed
`input`/`touch`/`id`/`reboot` builtins so a hostile payload either executes
(FAIL) or arrives byte-exact as typed text (PASS).
"""
import base64
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "manager"))
import app

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


HOSTILE = [
    "x$(touch MARKER_CMD)",
    "x`touch MARKER_BACKTICK`",
    "a;touch MARKER_SEMI",
    "a|touch MARKER_PIPE",
    "a&&touch MARKER_AND",
    "a\ntouch MARKER_NL",
    "$(touch MARKER_ONLY)",
    "a$(id>/dev/null)b",
    "x>MARKER_REDIR",
    "x<MARKER_IN",
    "a#touch MARKER_HASH",
    "~MARKER_TILDE",
    "a\\touch MARKER_BSLASH",
    "a!touch MARKER_BANG",
]

LEGIT = {
    "Player123": "input text Player123",
    "cool user": "input text cool%suser",
    "a.b+tag@example.com": "input text a.b+tag@example.com",
    "O'Brien": "input text 'O'\"'\"'Brien'",
    "user_name-99": "input text user_name-99",
}

_real_open = open


def drive_login(username):
    """Run the real _roblox_login_inner with fakes; return the script written."""
    written = {}

    def fake_open(path, mode="r", *a, **kw):
        # login-pwd-cleanup: pwd.txt is now staged via pwd.txt.tmp + rename,
        # so the tmp path must be faked too, or the login aborts on the
        # real (nonexistent) volume dir before writing auto_login.sh.
        # login-script-600: same for the script — it is now staged via
        # auto_login.sh.tmp + rename, so the tmp path must be faked too.
        if str(path).endswith(("auto_login.sh", "auto_login.sh.tmp",
                               "pwd.txt", "pwd.txt.tmp")):
            class W:
                def __init__(self):
                    self.buf = b"" if "b" in mode else ""

                def __enter__(self):
                    return self

                def __exit__(self, *e):
                    written[str(path)] = self.buf
                    return False

                def write(self, data):
                    if isinstance(self.buf, bytes):
                        data = data.encode() if isinstance(data, str) else data
                        self.buf += data
                    else:
                        self.buf += data
            return W()
        return _real_open(path, mode, *a, **kw)

    with mock.patch.object(app, "load_accounts",
                           lambda: {"rbx1": {"username": username, "password": "pw"}}), \
         mock.patch.object(app, "load_instances",
                           lambda: {"rbx1": {"provider": "local"}}), \
         mock.patch.object(app, "_send_input_cmds", lambda n, c: True), \
         mock.patch.object(app, "launch_roblox", lambda n: True), \
         mock.patch.object(app, "waydroid_exec", lambda n, c, timeout=30: (0, "")), \
         mock.patch("builtins.open", fake_open), \
         mock.patch("os.remove", lambda p: None), \
         mock.patch("os.replace", lambda s, d: written.__setitem__(str(d), written.pop(str(s), ""))), \
         mock.patch("os.chmod", lambda p, m: None), \
         mock.patch("time.sleep", lambda s: None):
        ok, msg = app._roblox_login_inner("rbx1")
    script = ""
    for path, content in written.items():
        if path.endswith("auto_login.sh"):
            script = content if isinstance(content, str) else content.decode()
    return ok, script


def run_sh_script(script):
    """Execute the whole login `script` under a real sh with stubbed commands.

    Returns (executed, typed): executed = sorted EXEC: markers the script
    managed to trigger; typed = the text arguments the stub `input` received
    for `text` invocations (base64 round-tripped so embedded newlines
    survive). `sleep` is stubbed; the password line's `cat` fails harmlessly
    (no pwd.txt in the sandbox) and its empty `input text ""` is filtered.
    """
    tmp = tempfile.mkdtemp(prefix="lue-")
    try:
        harness = (
            "#!/bin/sh\n"
            "D=\"$1\"; shift\n"
            "input() { if [ \"$1\" = text ]; then { printf '%s' \"$2\" | base64 | tr -d '\\n'; printf '\\n'; } >> \"$D/got\"; fi; }\n"
            "sleep() { :; }\n"
            "touch() { printf 'EXEC:touch\\n' >> \"$D/got\"; }\n"
            "id() { printf 'EXEC:id\\n' >> \"$D/got\"; }\n"
            "reboot() { printf 'EXEC:reboot\\n' >> \"$D/got\"; }\n"
            + script + "\n"
        )
        hp = os.path.join(tmp, "h.sh")
        with _real_open(hp, "w") as f:
            f.write(harness)
        os.chmod(hp, os.stat(hp).st_mode | stat.S_IXUSR)
        subprocess.run([hp, tmp], capture_output=True, timeout=10)
        gotp = os.path.join(tmp, "got")
        got = _real_open(gotp).read() if os.path.exists(gotp) else ""
        executed = sorted({l for l in got.splitlines() if l.startswith("EXEC:")})
        typed = [t for t in (base64.b64decode(l).decode("utf-8", "replace")
                             for l in got.splitlines()
                             if not l.startswith("EXEC:") and l) if t]
        return executed, typed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def username_typed_line(script):
    """The `input text` line for the username from a generated script (or None)."""
    for ln in script.splitlines():
        if ln.startswith("input text "):
            return ln
    return None


# ---- 1. hostile usernames: nothing executes, text arrives byte-exact ----
for h in HOSTILE:
    ok, script = drive_login(h)
    line = username_typed_line(script)
    if not ok or line is None:
        check(f"hostile {h!r}: login ran and emitted input text", False)
        continue
    executed, typed = run_sh_script(script)
    expected = h.replace(" ", "%s")
    check(f"hostile {h!r}: no command executed", executed == [])
    check(f"hostile {h!r}: typed text byte-exact", typed == [expected])

# ---- 2. the old escape really was exploitable (vuln proof, not shipped code) ----
# Space-free payload: the old escape's " "->"%s" mangling is irrelevant here.
old_line = "input text x$(id)"
executed, _ = run_sh_script(old_line)
check("old escape: same payload executes (proves the test catches the bug)",
      executed != [])

# ---- 3. legit usernames: exact lines + transparent typing ----
for user, want_line in LEGIT.items():
    ok, script = drive_login(user)
    line = username_typed_line(script)
    check(f"legit {user!r}: exact line", ok and line == want_line)
    executed, typed = run_sh_script(script)
    check(f"legit {user!r}: no execution", executed == [])
    check(f"legit {user!r}: typed as %s-encoded", typed == [user.replace(" ", "%s")])

# ---- 4. password line unchanged (already safe via "$P") ----
ok, script = drive_login("someuser")
check("password line byte-identical",
      'P=$(cat /data/media/0/jerusalem/pwd.txt); input text "$P"' in script.splitlines())

# ---- 5. source contract: ad-hoc escape gone, shlex.quote present ----
src = _real_open(os.path.join(os.path.dirname(__file__), "..", "..", "manager", "app.py")).read()
fn_src = src[src.index("def _roblox_login_inner"):]
fn_src = fn_src[:fn_src.index("\ndef ", 1)]
check("ad-hoc quote-strip escape removed",
      '.replace("\'", "")' not in fn_src and ".replace('\"', \"\")" not in fn_src)
check("shlex.quote at the sink", "shlex.quote(username.replace(\" \", \"%s\"))" in fn_src)
check("shlex imported at module level",
      re.search(r"^import shlex$", src, re.M) is not None)

# ---- 6. never-raises / edge behavior ----
for edge, want_ok in [("", False), (None, False)]:
    try:
        ok, _ = drive_login(edge)
        check(f"edge {edge!r}: returns (not raises), ok={want_ok}", ok == want_ok)
    except Exception:
        check(f"edge {edge!r}: never raises", False)
ok, script = drive_login("üñíçødé user")
executed, typed = run_sh_script(script)
check("unicode username: inert + byte-exact",
      executed == [] and typed == ["üñíçødé%suser"])

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
