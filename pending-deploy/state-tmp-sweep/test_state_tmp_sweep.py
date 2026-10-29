"""Unit tests for state tmp sweep + credential-state 600 hardening (manager/app.py).

Run: python3 test_state_tmp_sweep.py

REAL GAP FOUND by auditing the atomic-write idiom after the
waydroid-exec-cleanup / remote-b64-sweep sessions closed the
instance-volume staging files: every manager save_* writes FILE +
".tmp" then os.replace()s it into place, so a manager crash mid-write
leaves the tmp behind forever — and the state dir had NO sweep at
all. Two of the leftovers are credential-bearing: roblox_accounts.json
.tmp holds every fleet Roblox credential in plaintext,
tokens.json.tmp holds every dashboard session token, and
static_join_token.tmp holds the join token. The instance-volume sweep
never looked here, and _recover_batch_jobs() skips non-.json names,
so batch_jobs/<jid>.json.tmp files from a crash mid-journal were
orphaned the same way. A second, smaller hole rode along: save_tokens
never chmodded anything, so tokens.json sat world-readable permanently,
and save_accounts chmodded AFTER the replace, so the live file sat
world-readable between the two calls.

Fix (manager/app.py only, no agent change, no version cascade):
  * sweep_stale_state_tmp(data_dir, journal_dir) removes exactly the
    six known state files' .tmp siblings plus journal files matching
    <16-hex-jid>.json.tmp — flat listing, name-gated patterns only,
    never a live file, never recursive. Returns the removal count,
    never raises. main() runs it at startup before the server binds
    (no save can be in flight), next to the login-volume sweep.
  * save_accounts chmods its tmp to 600 BEFORE the replace (os.replace
    preserves the tmp's mode, so the live file is 600 from birth).
  * save_tokens gains the same chmod-before-replace (was never 600).

Hermetic: app.py loaded via importlib with JERUSALEM_DATA pointed at a
tmp dir; no docker, no network, no real DATA_DIR touched.
"""
import importlib.util
import os
import shutil
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = tempfile.mkdtemp(prefix="statetmp_")
DATA = os.path.join(BASE, "data")
JOURNAL = os.path.join(BASE, "journal")
os.makedirs(DATA)
os.makedirs(JOURNAL)
os.environ["JERUSALEM_DATA"] = DATA


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("statetmp_app", os.path.join(HERE, "..", "..", "manager", "app.py"))
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


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


STATE_NAMES = ["static_join_token", "providers.json", "instances.json",
               "roblox_accounts.json", "tokens.json",
               "roblox_login_results.json"]

print("== sweep: all six state tmps removed, live files untouched ==")
for n in STATE_NAMES:
    write(os.path.join(DATA, n), b"live")
    write(os.path.join(DATA, n + ".tmp"), b"stale")
n = app.sweep_stale_state_tmp(data_dir=DATA, journal_dir=JOURNAL)
check("removes all six state tmps (count 6)", n == 6)
check("no state tmp left",
      not any(os.path.exists(os.path.join(DATA, x + ".tmp")) for x in STATE_NAMES))
check("all six live files intact",
      all(open(os.path.join(DATA, x), "rb").read() == b"live" for x in STATE_NAMES))

print("== sweep: unrelated files untouched, no recursion ==")
write(os.path.join(DATA, "roblox_accounts.json.bak"), b"keep")
write(os.path.join(DATA, "random.tmp"), b"keep")
write(os.path.join(DATA, "notes.txt"), b"keep")
write(os.path.join(DATA, "sub", "tokens.json.tmp"), b"keep")
write(os.path.join(DATA, "tokens.json.tmp"), b"stale")
n = app.sweep_stale_state_tmp(data_dir=DATA, journal_dir=JOURNAL)
check("removes only the real state tmp (count 1)", n == 1)
check("bak file survives", os.path.exists(os.path.join(DATA, "roblox_accounts.json.bak")))
check("unrelated .tmp survives", os.path.exists(os.path.join(DATA, "random.tmp")))
check("notes.txt survives", os.path.exists(os.path.join(DATA, "notes.txt")))
check("subdir tmp survives (no recursion)",
      os.path.exists(os.path.join(DATA, "sub", "tokens.json.tmp")))

print("== sweep: missing / broken dirs never raise ==")
check("missing data dir -> 0",
      app.sweep_stale_state_tmp(data_dir=os.path.join(BASE, "nope"),
                                journal_dir=JOURNAL) == 0)
check("missing journal dir -> 0",
      app.sweep_stale_state_tmp(data_dir=DATA,
                                journal_dir=os.path.join(BASE, "nope")) == 0)
file_as_dir = os.path.join(BASE, "afile")
write(file_as_dir, b"x")
check("data dir is a file -> 0, never raises",
      app.sweep_stale_state_tmp(data_dir=file_as_dir, journal_dir=JOURNAL) == 0)
check("journal dir is a file -> 0, never raises",
      app.sweep_stale_state_tmp(data_dir=DATA, journal_dir=file_as_dir) == 0)

print("== sweep: journal tmps gated on jid ==")
JID = "0123456789abcdef"
write(os.path.join(JOURNAL, JID + ".json"), b"{}")
write(os.path.join(JOURNAL, JID + ".json.tmp"), b"stale")
write(os.path.join(JOURNAL, "notajid.json.tmp"), b"keep")
write(os.path.join(JOURNAL, "other.tmp"), b"keep")
JID2 = "fedcba9876543210"
os.makedirs(os.path.join(JOURNAL, JID2 + ".json.tmp"), exist_ok=True)
n = app.sweep_stale_state_tmp(data_dir=DATA, journal_dir=JOURNAL)
check("removes only the gated journal tmp (count 1)", n == 1)
check("live journal kept", os.path.exists(os.path.join(JOURNAL, JID + ".json")))
check("non-jid journal tmp kept", os.path.exists(os.path.join(JOURNAL, "notajid.json.tmp")))
check("other tmp kept", os.path.exists(os.path.join(JOURNAL, "other.tmp")))
check("valid-jid directory tmp kept (remove raises, never propagates)",
      os.path.isdir(os.path.join(JOURNAL, JID2 + ".json.tmp")))

print("== sweep: second sweep is a no-op ==")
check("idempotent (count 0)",
      app.sweep_stale_state_tmp(data_dir=DATA, journal_dir=JOURNAL) == 0)

print("== sweep: default dirs honored ==")
app.DATA_DIR = DATA
app.BATCH_JOURNAL_DIR = JOURNAL
write(os.path.join(DATA, "tokens.json.tmp"), b"stale")
write(os.path.join(JOURNAL, JID + ".json.tmp"), b"stale")
n = app.sweep_stale_state_tmp()
check("default-dir sweep removes both (count 2)", n == 2)

print("== save_accounts / save_tokens: 600 from birth, no tmp left ==")
old_umask = os.umask(0o022)
try:
    app.ACCOUNTS_FILE = os.path.join(DATA, "roblox_accounts.json")
    app.TOKENS_FILE = os.path.join(DATA, "tokens.json")
    app.save_accounts({"rbx1": {"username": "u", "password": "p"}})
    check("accounts live file is 600", mode(app.ACCOUNTS_FILE) == 0o600)
    check("accounts tmp gone",
          not os.path.exists(app.ACCOUNTS_FILE + ".tmp"))
    check("accounts content round-trips",
          app.load_accounts() == {"rbx1": {"username": "u", "password": "p"}})
    app._tokens = {"tok1": 9999999999}
    app.save_tokens()
    check("tokens live file is 600", mode(app.TOKENS_FILE) == 0o600)
    check("tokens tmp gone", not os.path.exists(app.TOKENS_FILE + ".tmp"))
    check("tokens content round-trips",
          open(app.TOKENS_FILE).read().find("tok1") >= 0)
finally:
    os.umask(old_umask)

print("== source contracts ==")
main_src = APP_SRC[APP_SRC.index("def main():"):]
check("main() runs the state sweep",
      "sweep_stale_state_tmp()" in main_src)
check("main() runs it before the server binds",
      main_src.index("sweep_stale_state_tmp()") < main_src.index("serve_forever"))
acc_src = APP_SRC[APP_SRC.index("def save_accounts"):]
acc_src = acc_src[:acc_src.index("def load_login_results")]
check("save_accounts chmods before the replace",
      acc_src.index("os.chmod(tmp") < acc_src.index("os.replace(tmp, ACCOUNTS_FILE)"))
tok_src = APP_SRC[APP_SRC.index("def save_tokens"):]
tok_src = tok_src[:tok_src.index("def run(")]
check("save_tokens chmods its tmp",
      "os.chmod(tmp, 0o600)" in tok_src)
check("sweep never globs (no glob call in function)",
      "glob." not in APP_SRC[APP_SRC.index("def sweep_stale_state_tmp"):
                            APP_SRC.index("def _roblox_login_inner")])

shutil.rmtree(BASE, ignore_errors=True)
print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
