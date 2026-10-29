"""Unit tests for the stale-frame gate (manager/app.py + wing/agent.py).

Gap: when the in-container screencap loop died, live.png stayed behind
with its last frame, and both screenshot getters returned that frozen
frame forever via their mtime caches (the cache key IS the mtime, and a
dead loop never changes it). Every downstream guard then saw a truthy
frame on every tick, so none of them could fire:
  - MJPEG _handle_ws: last_frame reset + 1s ping sent on every truthy
    frame -> the 15s frameless guard never tripped, the dashboard stall
    badge (3s, on ws messages) never showed.
  - H.264 broadcaster: frame_source kept returning a frame -> heartbeat
    encodes kept flowing -> its 15s frameless guard never tripped.
  - WebRTC track recv: _get_webrtc_frame kept returning a frame ->
    _last_fresh kept updating -> its 15s frameless failover never fired.
The viewer showed a frozen frame labelled live until the watchdog's
30s stale-stream restart (and only then, if it was watching).

Fix: capture_screenshot() (manager, local instances) and
screenshot_bytes_linux() (wing agent, remote instances) return None
when live.png is older than SCREENSHOT_STALE_SECS (10s). 10s sits
above the healthy loop cadence (0.12s fast / 2s idle, worst cycle ~7s
with a 5s screencap timeout) and below the 15s frameless guards and
the 30s watchdog stale check, so a healthy loop is never gated and a
dead loop yields None: pings/fragments stop, the stall badge shows,
and the frameless guards close/fail over as designed.

Run: python3 test_stale_frame_gate.py

Hermetic: the real app.py / agent.py modules are loaded; the volume
paths are hard-coded, so os.path.exists / os.path.getmtime /
builtins.open are mocked per case (scoped, restored after each call).
No docker, no network, no real files.
"""
import importlib.util
import io
import os
import random
import sys
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "..", "..")


def load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


app = load("stale_gate_app", os.path.join(REPO, "manager", "app.py"))
agent = load("stale_gate_agent", os.path.join(REPO, "wing", "agent.py"))

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


APP_SRC = open(os.path.join(REPO, "manager", "app.py")).read()
AGENT_SRC = open(os.path.join(REPO, "wing", "agent.py")).read()
WATCHDOG_SRC = open(os.path.join(REPO, "waydroid-image", "df-watchdog.sh")).read()
SCREENCAP_SRC = open(os.path.join(REPO, "waydroid-image", "screencap-loop.sh")).read()

# A real, >1000-byte PNG (random pixels compress poorly on purpose).
_rng = random.Random(1)
_raw = bytes(_rng.randrange(256) for _ in range(640 * 360 * 3))
try:
    from PIL import Image as _PILImage
    _buf = io.BytesIO()
    _PILImage.frombytes("RGB", (640, 360), _raw).save(_buf, "PNG")
    PNG_BYTES = _buf.getvalue()
except Exception:
    PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000
assert len(PNG_BYTES) > 1000, "test PNG must clear the >1000-byte guard"

NOW = 1700000000.0


def func_src(src, start_marker, end_marker):
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


print("== threshold constants ==")
check("agent VERSION bumped to 1.0.44 (so wing nodes self-update to the gate)",
      agent.VERSION == "1.0.45")
check("manager SCREENSHOT_STALE_SECS == 10",
      getattr(app, "SCREENSHOT_STALE_SECS", None) == 10)
check("agent SCREENSHOT_STALE_SECS == 10",
      getattr(agent, "SCREENSHOT_STALE_SECS", None) == 10)
check("manager and agent thresholds agree",
      app.SCREENSHOT_STALE_SECS == agent.SCREENSHOT_STALE_SECS)
check("threshold above idle cadence (2s) and below frameless guard (15s)",
      2 < app.SCREENSHOT_STALE_SECS < app.WS_FRAMELESS_TIMEOUT)
check("threshold below watchdog stale-stream check (30s)",
      app.SCREENSHOT_STALE_SECS < 30
      and 'if [ "$age" -gt 30 ]' in WATCHDOG_SRC)
check("screencap loop cadences are the documented ones",
      "FAST_SLEEP=0.12" in SCREENCAP_SRC and "IDLE_SLEEP=2" in SCREENCAP_SRC)

print("== gate placement in source ==")
app_cap = func_src(APP_SRC, "def capture_screenshot(name):",
                   "def ensure_cap_script(name):")
check("manager gate line present in capture_screenshot",
      "time.time() - mtime > SCREENSHOT_STALE_SECS" in app_cap)
check("manager gate precedes the mtime cache-hit return",
      app_cap.index("SCREENSHOT_STALE_SECS") < app_cap.index("cached[1]"))
check("manager gate returns None (not a stale frame)",
      "SCREENSHOT_STALE_SECS" in app_cap
      and "return None" in app_cap[app_cap.index("SCREENSHOT_STALE_SECS"):])
agent_cap = func_src(AGENT_SRC, "def screenshot_bytes_linux(name):",
                     "def _find_fifo(cname):")
check("agent gate line present in screenshot_bytes_linux",
      "time.time() - mtime > SCREENSHOT_STALE_SECS" in agent_cap)
check("agent gate precedes the mtime cache-hit return",
      agent_cap.index("SCREENSHOT_STALE_SECS") < agent_cap.index("cached[1]"))

print("== downstream propagation (source contracts) ==")
check("MJPEG local frame source is capture_screenshot",
      "return capture_screenshot(name)" in APP_SRC)
check("MJPEG remote frame source is get_remote_screenshot",
      "return get_remote_screenshot(wing, prov, name)" in APP_SRC)
check("H.264 frame_source uses the same two getters",
      APP_SRC.count("capture_screenshot(name)") >= 2
      and "get_remote_screenshot(wing, prov, name)" in APP_SRC)
check("WebRTC frame source uses the same two getters",
      "_get_webrtc_frame" in APP_SRC
      and "jpeg = capture_screenshot(name)" in APP_SRC)
check("agent screenshot route 404s when the getter returns None",
      "data = screenshot_bytes_linux(name)" in AGENT_SRC
      and 'return self._send(404, {"error": "no screenshot"})' in AGENT_SRC)
check("manager remote cache drops on a failed agent fetch",
      "_remote_shots.pop(key, None)" in APP_SRC)


# ---------------- mocked-filesystem harness ----------------
ORIG_OPEN = open


def run_manager(age, cached):
    """Call app.capture_screenshot with a fake live.png of the given age.

    cached: None, or bytes to pre-seed in _local_shot_cache for the
    fake mtime. Returns (result, live_png_open_count).
    """
    app._local_shot_cache.clear()
    mtime = NOW - age
    if cached is not None:
        app._local_shot_cache["rbx1"] = (mtime, cached)
    opens = {"n": 0}

    def fake_exists(p):
        return True

    def fake_getmtime(p):
        return mtime

    def fake_open(p, mode="r", *a, **k):
        if "live.png" in str(p):
            opens["n"] += 1
            return io.BytesIO(PNG_BYTES)
        return ORIG_OPEN(p, mode, *a, **k)

    with mock.patch("os.path.exists", fake_exists), \
            mock.patch("os.path.getmtime", fake_getmtime), \
            mock.patch("builtins.open", fake_open), \
            mock.patch("time.time", return_value=NOW):
        return app.capture_screenshot("rbx1"), opens["n"]


def run_agent(age, cached):
    agent._shot_cache.clear()
    mtime = NOW - age
    if cached is not None:
        agent._shot_cache["srt1"] = (mtime, cached)
    opens = {"n": 0}

    def fake_exists(p):
        return True

    def fake_getmtime(p):
        return mtime

    def fake_open(p, mode="r", *a, **k):
        if "live.png" in str(p):
            opens["n"] += 1
            return io.BytesIO(PNG_BYTES)
        return ORIG_OPEN(p, mode, *a, **k)

    with mock.patch("os.path.exists", fake_exists), \
            mock.patch("os.path.getmtime", fake_getmtime), \
            mock.patch("builtins.open", fake_open), \
            mock.patch("time.time", return_value=NOW):
        return agent.screenshot_bytes_linux("srt1"), opens["n"]


print("== manager capture_screenshot behaviour ==")
res, _ = run_manager(age=1, cached=b"cached-jpeg")
check("fresh frame + cached entry -> cached bytes returned",
      res == b"cached-jpeg")
res, _ = run_manager(age=10, cached=b"cached-jpeg")
check("boundary age == threshold (10s) is NOT stale -> cached returned",
      res == b"cached-jpeg")
res, opens = run_manager(age=11, cached=b"cached-jpeg")
check("age 11s + cached entry -> None (cache-hit must not mask staleness)",
      res is None)
check("stale cached case never reads the frozen file", opens == 0)
res, opens = run_manager(age=60, cached=None)
check("age 60s, no cache -> None", res is None)
check("stale uncached case never reads the frozen file", opens == 0)
res, _ = run_manager(age=-5, cached=b"cached-jpeg")
check("future mtime (negative age) is fresh -> cached returned",
      res == b"cached-jpeg")
res, _ = run_manager(age=1, cached=None)
check("fresh frame, no cache -> JPEG bytes (not the raw PNG)",
      res is not None and res.startswith(b"\xff\xd8"))
# Missing file: exists False everywhere.
with mock.patch("os.path.exists", lambda p: False), \
        mock.patch("time.time", return_value=NOW):
    app._local_shot_cache.clear()
    check("missing live.png -> None", app.capture_screenshot("rbx1") is None)

print("== agent screenshot_bytes_linux behaviour ==")
res, _ = run_agent(age=1, cached=b"cached-jpeg")
check("fresh frame + cached entry -> cached bytes returned",
      res == b"cached-jpeg")
res, _ = run_agent(age=10, cached=b"cached-jpeg")
check("boundary age == threshold (10s) is NOT stale -> cached returned",
      res == b"cached-jpeg")
res, opens = run_agent(age=11, cached=b"cached-jpeg")
check("age 11s + cached entry -> None (cache-hit must not mask staleness)",
      res is None)
check("stale cached case never reads the frozen file", opens == 0)
res, opens = run_agent(age=60, cached=None)
check("age 60s, no cache -> None", res is None)
res, _ = run_agent(age=1, cached=None)
check("fresh frame, no cache -> non-empty frame bytes",
      res is not None and len(res) > 1000)
check("agent rejects an invalid (traversal) name without touching the fs",
      agent.screenshot_bytes_linux("../x") is None)
with mock.patch("os.path.exists", lambda p: False), \
        mock.patch("time.time", return_value=NOW):
    agent._shot_cache.clear()
    check("missing live.png -> None", agent.screenshot_bytes_linux("srt1") is None)

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
