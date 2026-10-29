#!/usr/bin/env python3
"""Tests for screenshot-path-gate (manager only, no agent change).

GAP (security hardening, found by auditing the name-gate coverage after
t164): the MJPEG, H.264, and WebRTC stream routes all gate instance names
(route-name-gate), and the shell sinks (waydroid_exec, docker_inspect,
login, create, resize, action cores) gate them too — but
capture_screenshot(), the SHARED frame source for all three stream paths,
did not. It builds its docker-volume path straight from the name and
reads it, so a corrupted instances.json key (the same class the
t161-t164 sink gates were built for) reaching it from a future internal
caller would read a file outside its own instance directory: the fixed
live.png suffix still lets a traversal name pick another volume's frame,
or a path outside /var/lib/docker/volumes entirely. The dashboard routes
gate today; the sink itself was wrong regardless, same as the watchdog's
in t160.

FIX (manager/app.py only): capture_screenshot() returns None on an
invalid name before any path is built or the frame cache is touched —
indistinguishable from a missing frame, so the stream guards keep
treating it as "no frame". Valid names behave identically. No agent,
dashboard, or wire-protocol change.

Covers:
  A. regression proof (contract fact + path fact: the old volume-path
     construction normalizes a traversal name out of its own volume dir)
  B. real capture_screenshot (fake os.path + fake builtin open, real
     module otherwise): invalid names blocked with zero filesystem
     touches and an untouched frame cache
  C. valid names still work: fresh new-path frame, fresh old-path
     frame, mtime cache hit with no second read, missing frame -> None,
     stale frame -> None with no read
  D. source contracts (gate placement, sibling sink gates kept,
     stream call sites unchanged, snapshot parity)
  E. negative proof (reconstructed pre-fix path DOES escape the
     instance's volume dir for a traversal name, so the harness is not
     vacuous)
"""
import builtins
import importlib.util
import io
import os
import sys
import time

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


app = load("spg_app", APP)
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

FAKE_PNG = b"\x89PNG" + b"\x00" * 2000

# ---------------------------------------------------------------- A. regression
print("== A. regression proof (contract + path facts, no git needed) ==")
check("every attack-shaped name fails the gate contract",
      all(not app.valid_instance_name(n) for n in BAD_NAMES)
      and app.valid_instance_name("rbx1"),
      f"matrix={[app.valid_instance_name(n) for n in BAD_NAMES]}")
_raw = ("/var/lib/docker/volumes/wd-../../tmp/evil-data/_data/data/media/0/"
        "jerusalem/live.png")
_norm = os.path.normpath(_raw)
check("old volume-path construction normalizes out of the instance dir",
      _norm != _raw and "volumes/wd-" not in _norm
      and _norm.startswith("/var/lib/docker/volumes/tmp/evil-data")
      or "/tmp/evil-data/" in _norm,
      f"norm={_norm}")


# ---------------------------------------------------------------- B/C. real capture
print("== B/C. real capture_screenshot ==")


class FakePath:
    """Records exists/getmtime; never touches the real filesystem."""

    def __init__(self, mode="fresh"):
        self.mode = mode  # fresh | old | missing | stale
        self.exists_calls = []
        self.getmtime_calls = []
        # Fixed mtime: a real unchanged file reports the same mtime on
        # every call, which is what the mtime cache-hit depends on.
        self.fixed_mtime = time.time() - 1

    def exists(self, p):
        self.exists_calls.append(p)
        if self.mode == "missing":
            return False
        if self.mode == "old":
            return "droidfarm/live.png" in p
        return "jerusalem/live.png" in p

    def getmtime(self, p):
        self.getmtime_calls.append(p)
        if self.mode == "stale":
            return time.time() - 99999
        return self.fixed_mtime


class FakeOS:
    def __init__(self, mode="fresh"):
        self.path = FakePath(mode)


def run_capture(name, mode="fresh"):
    """Run the real function with a fake os.path + fake builtin open.

    Returns (result, fake_os, open_calls)."""
    fake = FakeOS(mode)
    open_calls = []
    real_os, real_open, real_pil = app.os, builtins.open, app.HAS_PIL
    app.HAS_PIL = False  # take the direct-return path, not the PIL path

    def fake_open(p, m="r", *a, **k):
        open_calls.append(str(p))
        if "wd-rbx1-data" in str(p) and "rb" in m:
            return io.BytesIO(FAKE_PNG)
        raise OSError("no such file")

    app.os, builtins.open = fake, fake_open
    try:
        app._local_shot_cache = {}
        res = app.capture_screenshot(name)
        cache = dict(app._local_shot_cache)
    finally:
        app.os, builtins.open, app.HAS_PIL = real_os, real_open, real_pil
    return res, fake, open_calls, cache


for name in BAD_NAMES:
    res, fake, open_calls, cache = run_capture(name)
    check(f"capture_screenshot blocks {name!r} with zero fs touches",
          res is None and fake.path.exists_calls == []
          and fake.path.getmtime_calls == [] and open_calls == []
          and cache == {},
          f"res={res} exists={fake.path.exists_calls} opens={open_calls}")

res, fake, open_calls, cache = run_capture("rbx1", "fresh")
# NOTE: the non-PIL path returns the raw PNG without populating the
# mtime cache (the cache write lives in the PIL branch only) — so this
# check asserts bytes + path, and the cache-hit check below uses a real
# PIL-decodable PNG on the real PIL path.
check("valid name reads the fresh new-path frame",
      res == FAKE_PNG and len(open_calls) == 1
      and "wd-rbx1-data/_data/data/media/0/jerusalem/live.png"
      in open_calls[0],
      f"res_len={None if res is None else len(res)} opens={open_calls}")
check("valid-name exists probes stay inside the instance volume",
      all(p.startswith("/var/lib/docker/volumes/wd-rbx1-data/")
          for p in fake.path.exists_calls),
      f"exists={fake.path.exists_calls}")

res, fake, open_calls, cache = run_capture("rbx1", "old")
check("valid name falls back to the old-path frame",
      res == FAKE_PNG and len(open_calls) == 1
      and "wd-rbx1-data/_data/data/media/0/droidfarm/live.png"
      in open_calls[0],
      f"opens={open_calls}")

# Mtime cache hit, on the REAL PIL path: the cache write lives in the
# PIL branch only, so feed a real decodable PNG (deterministic noise,
# >1000 bytes) and keep HAS_PIL as loaded. The second call must return
# the cached JPEG bytes without opening the file again. Drive the
# function directly here instead of via run_capture (which resets the
# cache and forces HAS_PIL=False).
from PIL import Image as _PILImage
import random as _random
_rng = _random.Random(1234)
_img = _PILImage.new("RGB", (200, 150))
_img.putdata([(_rng.randrange(256), _rng.randrange(256),
               _rng.randrange(256)) for _ in range(200 * 150)])
_buf = io.BytesIO()
_img.save(_buf, "PNG")
REAL_PNG = _buf.getvalue()
assert len(REAL_PNG) > 1000, len(REAL_PNG)
fake = FakeOS("fresh")
real_os, real_open = app.os, builtins.open
open_calls = []


def fake_open(p, m="r", *a, **k):
    open_calls.append(str(p))
    if "wd-rbx1-data" in str(p) and "rb" in m:
        return io.BytesIO(REAL_PNG)
    raise OSError("no such file")


app.os, builtins.open = fake, fake_open
try:
    app._local_shot_cache = {}
    first = app.capture_screenshot("rbx1")
    opens_after_first = len(open_calls)
    second = app.capture_screenshot("rbx1")
    opens_after_second = len(open_calls)
finally:
    app.os, builtins.open = real_os, real_open
check("mtime cache hit returns cached JPEG with no second read",
      first is not None and first[:2] == b"\xff\xd8"
      and second == first
      and opens_after_first == 1 and opens_after_second == 1,
      f"first_is_jpeg={first is not None and first[:2] == bytes([255, 216])} "
      f"opens={opens_after_first}->{opens_after_second}")

res, fake, open_calls, cache = run_capture("rbx1", "missing")
check("valid name with no frame returns None",
      res is None and open_calls == [] and cache == {},
      f"res={res} opens={open_calls}")

res, fake, open_calls, cache = run_capture("rbx1", "stale")
check("valid name with stale frame returns None without reading",
      res is None and open_calls == [] and cache == {},
      f"res={res} opens={open_calls}")

# ---------------------------------------------------------------- D. contracts
print("== D. source contracts ==")
shot = fn_src("capture_screenshot")
check("gate in capture_screenshot precedes cname build",
      shot.index("valid_instance_name(name)") <
      shot.index('cname = f"wd-{name}"'))
check("gate returns None (missing-frame signal)",
      "if not valid_instance_name(name):\n        return None" in shot)
for sib in ("waydroid_exec", "_send_input_cmds", "_roblox_login_inner",
            "create_local_instance", "resize_local_instance",
            "instance_single_action"):
    check(f"sibling sink gate kept: {sib}",
          "valid_instance_name(name)" in fn_src(sib))
check("MJPEG/H.264/WebRTC call sites unchanged",
      app_src.count("jpeg = capture_screenshot(name)") == 2
      and "return capture_screenshot(name)" in app_src)
check("remote-autologin app.py snapshot == manager/app.py",
      open(SNAP_APP, encoding="utf-8").read() == app_src)

# ---------------------------------------------------------------- E. negative proof
print("== E. negative proof ==")


def old_vol_path(name):
    """Reconstruction of the pre-fix path construction (no gate)."""
    cname = f"wd-{name}"
    return (f"/var/lib/docker/volumes/{cname}-data/_data/data/media/0/"
            f"jerusalem/live.png")


old = os.path.normpath(old_vol_path("../../tmp/evil"))
check("pre-fix path DOES escape the instance volume for traversal name",
      not old.startswith("/var/lib/docker/volumes/wd-")
      and "/tmp/evil-data/" in old,
      f"old={old}")

print(f"PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
