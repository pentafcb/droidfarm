#!/usr/bin/env python3
"""remote-frame-cache: one wing screenshot fetch per instance per 100ms.

Gap: the three streaming paths each fetched remote (wing) screenshots with
their own HTTP GET per frame — the MJPEG _handle_ws per-viewer thread and
the WebRTC track per recv did N x 20 fetches/sec for N viewers on one
remote instance (the H.264 broadcaster was already shared across its own
viewers but still ran its own fetch). The local path was already
mtime-cached inside capture_screenshot; the remote path had no cache at all.

Fix (manager/app.py): get_remote_screenshot(wing, prov_id, name) — a
100ms-TTL cache keyed by (prov_id, name), used by all three call sites.
On fetch failure the entry is dropped and None returned (never serve
stale: the 15s frameless guards on all three paths must see a dead source
as None). Best-effort, never raises.

Hermetic: the real helper is driven with a faked app.wing_request; call
sites are verified by source contract + a functional drive of
_get_webrtc_frame's remote branch.
"""
import inspect
import os
import sys
import threading
import time
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


def reset():
    with app._remote_shots_lock:
        app._remote_shots.clear()


WING = {"url": "http://node:8090", "agent_key": "k"}
BIG = b"\xff" * 2000


class FakeWing:
    def __init__(self, script=None):
        # script: list of (code, data) or exceptions, consumed in order;
        # default is endless 200/BIG.
        self.script = list(script) if script else []
        self.calls = 0

    def __call__(self, wing, method, path, **kw):
        self.calls += 1
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return 200, BIG


# ------------------------------------------------------------------ basics
print("== cache hit / ttl / miss ==")
reset()
fw = FakeWing()
with mock.patch.object(app, "wing_request", fw):
    a = app.get_remote_screenshot(WING, "p1", "srt1")
    b = app.get_remote_screenshot(WING, "p1", "srt1")
    check("hit returns bytes", a == BIG and b == BIG)
    check("second call within TTL does not re-fetch", fw.calls == 1)
    check("hit returns the identical cached object", a is b)
    time.sleep(app._REMOTE_SHOT_TTL + 0.05)
    c = app.get_remote_screenshot(WING, "p1", "srt1")
    check("TTL expiry re-fetches", fw.calls == 2 and c == BIG)

# ------------------------------------------------------------------ failure
print("== failure drops entry, never serves stale ==")
reset()
fw = FakeWing(script=[(200, BIG), (500, b"err"), (200, BIG)])
with mock.patch.object(app, "wing_request", fw):
    check("prime ok", app.get_remote_screenshot(WING, "p1", "srt1") == BIG)
    time.sleep(app._REMOTE_SHOT_TTL + 0.05)
    check("failed fetch -> None (not stale bytes)",
          app.get_remote_screenshot(WING, "p1", "srt1") is None)
    check("entry dropped on failure",
          ("p1", "srt1") not in app._remote_shots)
    check("next call re-fetches after failure",
          app.get_remote_screenshot(WING, "p1", "srt1") == BIG
          and fw.calls == 3)

print("== short / wrong payload not cached ==")
reset()
fw = FakeWing(script=[(200, b"tiny"), (200, BIG)])
with mock.patch.object(app, "wing_request", fw):
    check("short body -> None", app.get_remote_screenshot(WING, "p1", "s") is None)
    check("short body not cached (re-fetch next call)",
          app.get_remote_screenshot(WING, "p1", "s") == BIG and fw.calls == 2)

print("== raising wing_request never raises ==")
reset()
fw = FakeWing(script=[RuntimeError("boom"), (200, BIG)])
with mock.patch.object(app, "wing_request", fw):
    try:
        r = app.get_remote_screenshot(WING, "p1", "srt1")
        check("raising fetch -> None, no raise", r is None)
    except Exception:
        check("raising fetch -> None, no raise", False)
    check("recovers on next call",
          app.get_remote_screenshot(WING, "p1", "srt1") == BIG)

print("== garbage wing never raises ==")
reset()
with mock.patch.object(app, "wing_request", FakeWing(script=[(200, BIG)])):
    for bad in (None, {}, {"url": "http://x"}):
        try:
            r = app.get_remote_screenshot(bad, "p1", "srt1")
            check(f"wing={bad!r} -> None-ish, no raise", r in (None, BIG))
        except Exception:
            check(f"wing={bad!r} -> None-ish, no raise", False)

# ------------------------------------------------------------------ keys
print("== per-key isolation ==")
reset()
fw = FakeWing()
with mock.patch.object(app, "wing_request", fw):
    app.get_remote_screenshot(WING, "p1", "srt1")
    app.get_remote_screenshot(WING, "p2", "srt1")
    app.get_remote_screenshot(WING, "p1", "srt2")
    app.get_remote_screenshot(WING, "p1", "srt1")
    check("3 distinct keys -> 3 fetches, 4th call hits",
          fw.calls == 3)

print("== stale entries pruned ==")
reset()
with app._remote_shots_lock:
    app._remote_shots[("p9", "old")] = [time.monotonic() - 61, BIG]
fw = FakeWing()
with mock.patch.object(app, "wing_request", fw):
    app.get_remote_screenshot(WING, "p1", "new")
    with app._remote_shots_lock:
        check("61s-stale entry pruned on write", ("p9", "old") not in app._remote_shots)
        check("fresh entry kept", ("p1", "new") in app._remote_shots)

# ------------------------------------------------------------------ threads
print("== concurrent viewers share fetches ==")
reset()
fw = FakeWing()
results = []
with mock.patch.object(app, "wing_request", fw):
    def viewer():
        for _ in range(30):
            results.append(app.get_remote_screenshot(WING, "p1", "srt1"))
    ts = [threading.Thread(target=viewer) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("all 300 calls got bytes", all(r == BIG for r in results))
    # worst case every thread misses once before the first store lands
    check("fetch count bounded by thread count", fw.calls <= 10)

# ------------------------------------------------------------------ contracts
print("== call-site contracts ==")
src_ws = inspect.getsource(app.Handler._handle_ws)
check("mjpeg get_frame uses the shared helper",
      "get_remote_screenshot(wing, prov, name)" in src_ws)
check("mjpeg get_frame has no direct screenshot fetch",
      'wing_request(wing, "GET"' not in src_ws)

src_h264 = inspect.getsource(app._get_h264_broadcaster)
check("h264 frame_source uses the shared helper",
      "get_remote_screenshot(wing, prov, name)" in src_h264)
check("h264 frame_source has no direct screenshot fetch",
      'wing_request(wing, "GET"' not in src_h264)

src_wr = inspect.getsource(app._get_webrtc_frame)
check("webrtc frame getter uses the shared helper",
      "get_remote_screenshot(wing, provider, name)" in src_wr)
check("webrtc frame getter has no direct screenshot fetch",
      'wing_request(wing, "GET"' not in src_wr)

print("== helper shape ==")
check("TTL is 0.1s", app._REMOTE_SHOT_TTL == 0.1)
check("helper documented never-raises",
      "never raises" in (inspect.getsource(app.get_remote_screenshot).lower()))

# functional: drive _get_webrtc_frame's remote branch through the cache
print("== functional: webrtc remote branch ==")
try:
    from PIL import Image
    import io as _io
    import random as _random
    _random.seed(7)
    _img = Image.new("RGB", (320, 240))
    _img.putdata([( _random.randrange(256), _random.randrange(256),
                    _random.randrange(256)) for _ in range(320 * 240)])
    _buf = _io.BytesIO()
    _img.save(_buf, format="JPEG", quality=90)
    REAL_JPEG = _buf.getvalue()
except Exception:
    REAL_JPEG = None
reset()
if REAL_JPEG and len(REAL_JPEG) > 1000:
    fw = FakeWing(script=[(200, REAL_JPEG)])
    with mock.patch.object(app, "wing_request", fw), \
         mock.patch.object(app, "load_providers", lambda: {"p1": WING}):
        f1 = app._get_webrtc_frame("srt1", "p1")
        f2 = app._get_webrtc_frame("srt1", "p1")
        check("webrtc remote returns decoded frames", f1 is not None and f2 is not None)
        check("webrtc remote shares the cache (1 fetch)", fw.calls == 1)
        w, h, rgb = f1
        check("frame tuple shape sane", w > 0 and h > 0 and len(rgb) == w * h * 3)
else:
    check("test jpeg generated", False)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
