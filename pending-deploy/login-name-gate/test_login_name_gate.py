#!/usr/bin/env python3
"""Tests for login-name-gate (manager only, no agent change).

GAP (malfunction/security hardening, found by auditing the name-gate
coverage after t160): the manager's HTTP routes all gate instance names
(route-name-gate), and waydroid_exec / docker_inspect gate them at the
sink — but TWO login-path sinks did not:

  1. _roblox_login_inner: writes the cleartext password file (pwd.txt)
     and the login script (auto_login.sh) into a volume path built
     straight from the name, then taps the login coordinates. It is
     reached from the auto-login watcher and the batch-login worker,
     which iterate instances.json keys — a corrupted key (the same
     class the t154/t155/t156 record-shape guards were built for)
     could drive those writes and taps. The dashboard Login route
     gates its name, but the internal callers never did.
  2. _send_input_cmds: builds its FIFO path straight from the name
     and opens it, with the same internal callers (login inner,
     launch_roblox). Its sibling waydroid_exec already had the gate;
     this helper did not.

FIX (manager/app.py only): both functions return fail-closed on an
invalid name before touching the filesystem or any container —
_roblox_login_inner returns (False, "instance not found") and
_send_input_cmds returns False. Valid names behave identically; the
roblox_login() wrapper still records the outcome (record_login_result
sanitizes names). No agent, dashboard, or wire-protocol change.

Covers:
  A. regression proof (contract fact: every attack-shaped name fails
     valid_instance_name; path fact: the old FIFO-path construction
     normalizes a traversal name out of its per-instance directory)
  B. real _send_input_cmds (FakeOS FIFO + stubbed run): invalid names
     blocked with zero filesystem touches, valid names still reach the
     FIFO / fallback paths
  C. real _roblox_login_inner / roblox_login (stubbed loads): invalid
     names blocked before any write or wing call even with a valid
     account record and a remote-looking provider; valid-name full run
     still completes and cleans up its password file + script
  D. source contracts (gate placement, wrapper recording, sibling
     gates kept, snapshot parity)
  E. negative proof (reconstructed pre-fix FIFO path DOES touch the
     filesystem for a traversal name, so the harness is not vacuous)
"""
import importlib.util
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
SNAP_APP = os.path.join(REPO, "pending-deploy", "remote-autologin", "app.py")

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


app = load("lng_app", APP)
app_src = open(APP, encoding="utf-8").read()


def fn_src(fname, src=None):
    src = app_src if src is None else src
    start = src.index(f"def {fname}(")
    try:
        end = src.index("\ndef ", start + 1)
    except ValueError:
        end = len(src)
    return src[start:end]


BAD_NAMES = ["", "../x", "../../tmp/evil", "a/b", "x;y",
             "$(id)", "rbx 1", "a" * 33, "`id`", "rbx1\nrbx2"]

# ---------------------------------------------------------------- A. regression
print("== A. regression proof (contract + path facts, no git needed) ==")
check("every attack-shaped name fails the gate contract",
      all(not app.valid_instance_name(n) for n in BAD_NAMES)
      and app.valid_instance_name("rbx1"),
      f"matrix={[app.valid_instance_name(n) for n in BAD_NAMES]}")
# The pre-fix construction glued the raw name into the path: a traversal
# name normalizes OUT of its per-instance directory, which is exactly
# the filesystem influence the gate removes.
_raw = ("/var/lib/docker/volumes/wd-../../tmp/evil-data/_data/data/media/0/"
        "jerusalem/input.fifo")
_norm = os.path.normpath(_raw)
check("old FIFO-path construction normalizes out of the instance dir",
      _norm != _raw and "volumes/tmp/evil-data" in _norm,
      f"norm={_norm}")


# ---------------------------------------------------------------- B. _send_input_cmds
print("== B. _send_input_cmds ==")


class FakeOS:
    """Records FIFO opens/writes; never touches the real filesystem."""
    O_WRONLY = os.O_WRONLY
    O_NONBLOCK = os.O_NONBLOCK

    def __init__(self, fail_open=True):
        self.fail_open = fail_open
        self.opens = []
        self.writes = []

    def open(self, path, flags):
        self.opens.append(path)
        if self.fail_open:
            raise OSError("no fifo")
        return 99

    def write(self, fd, data):
        self.writes.append(data)
        return len(data)

    def close(self, fd):
        pass


real_os, real_run = app.os, app.run
try:
    for name in BAD_NAMES:
        fake = FakeOS()
        app.os, app.run = fake, lambda *a, **k: (1, "", "")
        try:
            res = app._send_input_cmds(name, ["input tap 5 6"])
        finally:
            app.os, app.run = real_os, real_run
        check(f"_send_input_cmds blocks {name!r} with zero opens",
              res is False and fake.opens == [],
              f"res={res} opens={fake.opens}")

    # Valid name, FIFO missing: one open attempt, then the (stubbed)
    # waydroid_exec fallback fails -> False. Proves valid names are NOT
    # blocked by the gate, they just have nowhere to write.
    fake = FakeOS(fail_open=True)
    app.os, app.run = fake, lambda *a, **k: (1, "", "")
    try:
        res = app._send_input_cmds("rbx1", ["input tap 5 6"])
    finally:
        app.os, app.run = real_os, real_run
    check("valid name still attempts FIFO then fallback",
          res is False and len(fake.opens) == 1
          and fake.opens[0].endswith("wd-rbx1-data/_data/data/media/0/"
                                     "jerusalem/input.fifo"),
          f"res={res} opens={fake.opens}")

    # Valid name, FIFO present: one compound write, True.
    fake = FakeOS(fail_open=False)
    app.os, app.run = fake, lambda *a, **k: (1, "", "")
    try:
        res = app._send_input_cmds("rbx1", ["input tap 5 6", "sleep 1"])
    finally:
        app.os, app.run = real_os, real_run
    check("valid name writes one compound FIFO line",
          res is True and fake.writes == [b"input tap 5 6; sleep 1\n"],
          f"res={res} writes={fake.writes}")
finally:
    app.os, app.run = real_os, real_run

# ---------------------------------------------------------------- C. login inner
print("== C. _roblox_login_inner / roblox_login ==")


def run_login(name, acct=True):
    """Run the real inner function with stubbed loads; returns
    (result, wing_calls, tmpdir_tree)."""
    tmp = tempfile.mkdtemp(prefix="lng-")
    wing_calls = []
    real = (app.load_accounts, app.load_instances, app.wing_request,
            app.time, app.VOLUMES_BASE, app.run)
    app.load_accounts = lambda: ({name: {"username": "u1", "password": "p1",
                                         "auto_login": True}} if acct
                                  else {})
    app.load_instances = lambda: {name: {"provider": "p_deadbeef"}}
    app.wing_request = lambda *a, **k: wing_calls.append(a) or (500, {})
    app.time = types.SimpleNamespace(sleep=lambda *a: None)
    app.VOLUMES_BASE = tmp
    app.run = lambda *a, **k: (1, "", "")
    try:
        res = app._roblox_login_inner(name)
    finally:
        (app.load_accounts, app.load_instances, app.wing_request,
         app.time, app.VOLUMES_BASE, app.run) = real
    tree = []
    for root, _dirs, files in os.walk(tmp):
        tree.extend(os.path.join(root, f) for f in files)
    return res, wing_calls, tree


for name in BAD_NAMES:
    res, wing, tree = run_login(name, acct=True)
    check(f"login inner blocks {name!r} before writes/wing",
          res == (False, "instance not found") and wing == [] and tree == [],
          f"res={res} wing={wing} tree={tree}")

res, _wing, _tree = run_login("rbx1", acct=False)
check("valid name without credentials still reports no credentials",
      res == (False, "no credentials saved for this instance"),
      f"res={res}")

# Valid-name full run: pre-create the volume dir so the pwd/script
# writes succeed; the run must return True and leave nothing behind
# (the finally block discards both files).
tmp = tempfile.mkdtemp(prefix="lng-ok-")
jer = os.path.join(tmp, "wd-rbx1-data", "_data", "data", "media", "0",
                    "jerusalem")
os.makedirs(jer)
real = (app.load_accounts, app.load_instances, app.time,
        app.VOLUMES_BASE, app.run)
app.load_accounts = lambda: {"rbx1": {"username": "u1", "password": "p1",
                                      "auto_login": True}}
app.load_instances = lambda: {"rbx1": {"provider": "local"}}
app.time = types.SimpleNamespace(sleep=lambda *a: None)
app.VOLUMES_BASE = tmp
app.run = lambda *a, **k: (1, "", "")
try:
    res = app._roblox_login_inner("rbx1")
finally:
    (app.load_accounts, app.load_instances, app.time,
     app.VOLUMES_BASE, app.run) = real
leftovers = os.listdir(jer)
check("valid-name login still runs full sequence and cleans up",
      res == (True, "login sequence sent - check if login succeeded")
      and leftovers == [],
      f"res={res} leftovers={leftovers}")

# Wrapper records the gated outcome (stubbed recorder, no writes).
for by in ("auto", "batch"):
    recorded = []
    real = (app.record_login_result, app.VOLUMES_BASE)
    app.record_login_result = lambda n, o, m, by="manual": recorded.append(
        (n, o, m, by))
    app.VOLUMES_BASE = tempfile.mkdtemp(prefix="lng-wrap-")
    try:
        res = app.roblox_login("../../tmp/evil", by=by)
    finally:
        app.record_login_result, app.VOLUMES_BASE = real
    check(f"wrapper records gated outcome by={by}",
          res == (False, "instance not found")
          and recorded == [("../../tmp/evil", False, "instance not found",
                            by)],
          f"res={res} recorded={recorded}")

# ---------------------------------------------------------------- D. contracts
print("== D. source contracts ==")
inner = fn_src("_roblox_login_inner")
check("gate in inner precedes account load",
      inner.index("valid_instance_name(name)") <
      inner.index("accounts = load_accounts()"))
sender = fn_src("_send_input_cmds")
check("gate in _send_input_cmds precedes cname build",
      sender.index("valid_instance_name(name)") <
      sender.index('cname = f"wd-{name}"'))
check("wrapper still records outcome",
      "record_login_result(name, ok, msg, by)" in fn_src("roblox_login"))
check("waydroid_exec sink gate kept",
      "if not valid_instance_name(name):" in fn_src("waydroid_exec"))
check("remote-autologin app.py snapshot == manager/app.py",
      open(SNAP_APP, encoding="utf-8").read() == app_src)

# ---------------------------------------------------------------- E. negative proof
print("== E. negative proof ==")


def old_send_input_cmds(name, cmds, fake_os):
    """Reconstruction of the pre-fix helper (no gate): FIFO path built
    straight from the name and opened."""
    cname = f"wd-{name}"
    fifo_path = (f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/"
                 f"jerusalem/input.fifo")
    compound = "; ".join(cmds)
    try:
        fd = fake_os.open(fifo_path, fake_os.O_WRONLY | fake_os.O_NONBLOCK)
        try:
            fake_os.write(fd, (compound + "\n").encode())
        finally:
            fake_os.close(fd)
        return True
    except OSError:
        return False


fake = FakeOS(fail_open=False)
old_send_input_cmds("../../tmp/evil", ["input tap 5 6"], fake)
check("pre-fix path DOES touch filesystem for traversal name",
      len(fake.opens) == 1 and "tmp/evil" in fake.opens[0],
      f"opens={fake.opens}")

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
