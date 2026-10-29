#!/usr/bin/env python3
"""Tests for the adaptive-screencap package.

The in-container screencap loop captures fast (0.12s) while a viewer is
attached and idles at 2s otherwise. Signalling is via a `stream_wanted`
marker file whose mtime the loop checks (fresh <30s = viewer attached).

Covers:
  A. manager viewer counting: open/close, refcount, last-out clears marker
  B. manager never-raises on garbage names / remote providers
  C. manager retouch (15s poller keeps long-lived viewers fast)
  D. manager wiring contract: hooks in _handle_ws/_handle_ws_h264/finally,
     resource_poller calls stream_want_retouch
  E. wing agent 1.0.6: fetch note + sweep, VERSION, wiring contract
  F. entrypoint/Dockerfile/loop-script wiring contract
"""
import importlib.util
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP = os.path.join(REPO, "manager", "app.py")
AGENT = os.path.join(REPO, "wing", "agent.py")
LOOP = os.path.join(REPO, "waydroid-image", "screencap-loop.sh")
ENTRYPOINT = os.path.join(REPO, "waydroid-image", "entrypoint.sh")
BOOT_COMPLETE = os.path.join(REPO, "waydroid-image", "boot-complete.sh")
DOCKERFILE = os.path.join(REPO, "waydroid-image", "Dockerfile")

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


def load(path, modname):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


app = load(APP, "df_app_adaptive")
agent = load(AGENT, "df_agent_adaptive")
app_src = open(APP).read()
agent_src = open(AGENT).read()

# ---------------------------------------------------------------- A. counting
tmp = tempfile.mkdtemp()
BASE = os.path.join(tmp, "volumes")
os.makedirs(BASE)
app._STREAM_WANT_BASE = BASE  # redirect the module constant for hermetic tests


def marker(name):
    return os.path.join(BASE, f"wd-{name}-data", "_data",
                        "data", "media", "0", "jerusalem", "stream_wanted")


def reset():
    with app._stream_viewers_lock:
        app._stream_viewers.clear()
    for root, _d, files in os.walk(BASE):
        for f in files:
            os.remove(os.path.join(root, f))


reset()
check("open creates marker dir chain + file",
      (app._touch_stream_want("rbx1", BASE), os.path.exists(marker("rbx1")))[1])
app.stream_viewer_open("local", "rbx1")
check("viewer open keeps marker", os.path.exists(marker("rbx1")))
app.stream_viewer_open("local", "rbx1")
with app._stream_viewers_lock:
    n = app._stream_viewers.get(("local", "rbx1"), 0)
check("double open refcounts to 2", n == 2, f"got {n}")
app.stream_viewer_close("local", "rbx1")
with app._stream_viewers_lock:
    n = app._stream_viewers.get(("local", "rbx1"), 0)
check("first close leaves count 1 + marker", n == 1 and os.path.exists(marker("rbx1")),
      f"n={n}")
app.stream_viewer_close("local", "rbx1")
with app._stream_viewers_lock:
    gone = ("local", "rbx1") not in app._stream_viewers
check("last close clears registry + removes marker",
      gone and not os.path.exists(marker("rbx1")))
app.stream_viewer_close("local", "rbx1")  # close with no open
check("close with no open never raises, no marker",
      not os.path.exists(marker("rbx1")))

# ---------------------------------------------------------------- B. garbage / remote
reset()
for gname in ["../x", "", None, "a" * 200, "rbx;1", "rbx 1"]:
    try:
        app.stream_viewer_open("local", gname)
        app.stream_viewer_close("local", gname)
        app._touch_stream_want(gname, BASE)
        app._clear_stream_want(gname, BASE)
    except Exception as e:  # noqa: BLE001
        bad(f"garbage name never raises ({gname!r})", repr(e))
        break
else:
    ok("garbage names never raise")
leftovers = []
for root, _d, files in os.walk(BASE):
    leftovers.extend(files)
check("garbage names create no marker files", leftovers == [], repr(leftovers))

reset()
app.stream_viewer_open("srt", "srt1")
app.stream_viewer_close("srt", "srt1")
with app._stream_viewers_lock:
    tracked = any(k[0] != "local" for k in app._stream_viewers)
check("remote provider is a no-op (agent tracks its own fetches)", not tracked)
check("remote provider writes no local marker",
      not os.path.exists(os.path.join(BASE, "wd-srt1-data")))

# ---------------------------------------------------------------- C. retouch
reset()
app.stream_viewer_open("local", "rbx2")
p = marker("rbx2")
old = time.time() - 60
os.utime(p, (old, old))
app.stream_want_retouch()
check("retouch refreshes marker mtime for active viewers",
      time.time() - os.path.getmtime(p) < 5,
      f"age={time.time() - os.path.getmtime(p):.1f}s")
reset()
app.stream_want_retouch()
leftovers = []
for _root, _d, files in os.walk(BASE):
    leftovers.extend(files)
check("retouch with no viewers creates nothing", leftovers == [], repr(leftovers))

# threadsafety smoke
reset()
errs = []


def churn():
    try:
        for _ in range(50):
            app.stream_viewer_open("local", "rbx9")
            app.stream_viewer_close("local", "rbx9")
    except Exception as e:  # noqa: BLE001
        errs.append(e)


threads = [threading.Thread(target=churn) for _ in range(10)]
[t.start() for t in threads]
[t.join() for t in threads]
with app._stream_viewers_lock:
    empty = not app._stream_viewers
check("10 threads x 50 open/close: no errors, registry empty, no marker",
      not errs and empty and not os.path.exists(marker("rbx9")),
      repr(errs[:1]))

# ---------------------------------------------------------------- D. manager wiring
check("_handle_ws opens viewer after handshake",
      "stream_viewer_open(prov, name)" in app_src)
hws = app_src[app_src.index("def _handle_ws(self, name)"):]
hws = hws[:hws.index("def _handle_ws_h264")]
check("_handle_ws finally closes viewer",
      hws.rstrip().count("stream_viewer_close(prov, name)") >= 1)
hh264 = app_src[app_src.index("def _handle_ws_h264(self, name)"):]
hh264 = hh264[:hh264.index("def _authed_ws")]
check("_handle_ws_h264 opens viewer after subscribe",
      "stream_viewer_open(prov, name)" in hh264)
check("_handle_ws_h264 finally closes viewer + unsubscribes",
      "stream_viewer_close(prov, name)" in hh264 and "bcast.unsubscribe(sub)" in hh264)
check("resource_poller calls stream_want_retouch",
      "stream_want_retouch()" in app_src)
check("stale threshold is 30s", "_STREAM_WANT_STALE = 30" in app_src)
check("remote branch documented as agent-owned",
      "wing agent tracks its own" in app_src)

# ---------------------------------------------------------------- E. agent
check("agent VERSION is 1.0.6+ (1.0.7 adds node disk/log guard, 1.0.8 adds HTTP server bounds, 1.0.9 adds selfwatch-events endpoint)", agent.VERSION in ("1.0.6", "1.0.7", "1.0.8", "1.0.9", "1.0.10", "1.0.11", "1.0.12", "1.0.13", "1.0.14", "1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20", "1.0.21", "1.0.22", "1.0.23", "1.0.24", "1.0.25", "1.0.26", "1.0.27", "1.0.28", "1.0.29", "1.0.30", "1.0.31", "1.0.32", "1.0.33", "1.0.34", "1.0.35", "1.0.36", "1.0.37", "1.0.38", "1.0.39", "1.0.40", "1.0.41", "1.0.42", "1.0.43", "1.0.44", "1.0.45"), repr(agent.VERSION))
atmp = tempfile.mkdtemp()
ABASE = os.path.join(atmp, "volumes")
os.makedirs(ABASE)


def amarker(name):
    return os.path.join(ABASE, f"wd-{name}-data", "_data",
                        "data", "media", "0", "jerusalem", "stream_wanted")


agent._STREAM_WANT_BASE_AGENT = ABASE  # hermetic redirect
agent._note_screenshot_fetch("srt1", ABASE)
check("fetch note creates marker", os.path.exists(amarker("srt1")))
check("fetch note records timestamp",
      abs(agent._SHOT_FETCH.get("srt1", 0) - time.time()) < 5)
old = time.time() - 60
os.utime(amarker("srt1"), (old, old))
agent._STREAM_WANT_BASE_AGENT = ABASE  # hermetic redirect
agent._note_screenshot_fetch("srt1", ABASE)
check("fetch note re-touches marker",
      time.time() - os.path.getmtime(amarker("srt1")) < 5)
agent._sweep_stream_want("srt1", ABASE)
check("sweep keeps marker while fetches recent", os.path.exists(amarker("srt1")))
with agent._SHOT_FETCH_LOCK:
    agent._SHOT_FETCH["srt1"] = time.time() - 120
agent._sweep_stream_want("srt1", ABASE)
check("sweep removes marker after 90s idle", not os.path.exists(amarker("srt1")))
agent._sweep_stream_want("never-seen", ABASE)
check("sweep on unknown instance never raises / creates nothing",
      not os.path.exists(amarker("never-seen")))
for gname in ["../x", "", None, "srt;1"]:
    try:
        agent._note_screenshot_fetch(gname, ABASE)
        agent._sweep_stream_want(gname, ABASE)
    except Exception as e:  # noqa: BLE001
        bad(f"agent garbage name never raises ({gname!r})", repr(e))
        break
else:
    ok("agent garbage names never raise")
check("screenshot route notes the fetch",
      "_note_screenshot_fetch(name)" in agent_src
      and "valid_agent_instance_name(name)" in agent_src)
check("selfwatch sweeps the marker",
      "_sweep_stream_want(name, vol_base)" in agent_src)
check("agent stale threshold is 90s", "_STREAM_WANT_STALE_AGENT = 90" in agent_src)

# ---------------------------------------------------------------- F. image wiring
loop_src = open(LOOP).read()
ep_src = open(ENTRYPOINT).read()
df_src = open(DOCKERFILE).read()
check("loop: fast sleep 0.12", "FAST_SLEEP=0.12" in loop_src)
check("loop: idle sleep 2", "IDLE_SLEEP=2" in loop_src)
check("loop: stale threshold 30", "STALE_AFTER=30" in loop_src)
check("loop: reads stream_wanted marker", 'WANT="$D/stream_wanted"' in loop_src)
check("loop: atomic rename preserved",
      'mv "$D/live.tmp.png" "$D/live.png"' in loop_src)
check("loop: dir overridable for tests", "SCREENCAP_DIR:-" in loop_src)
check("loop: fresh-marker check degrades safe on bad clock",
      "*[!0-9]*) echo 0" in loop_src)
check("Dockerfile COPYs loop to /opt",
      "COPY screencap-loop.sh /opt/screencap-loop.sh" in df_src)
bc_src = open(BOOT_COMPLETE).read()
check("boot-complete installs canonical loop from /opt",
      "cp -f \"$OPT/screencap-loop.sh\" \"$S/screencap-loop.sh\"" in bc_src)
check("boot-complete WARNs + falls back when /opt copy missing",
      "/opt/screencap-loop.sh missing" not in bc_src and
      "screencap-loop.sh missing - legacy inline fast-only loop" in bc_src)
check("boot-complete starts the loop from the data dir",
      'start_helper "screencap-loop.sh"' in bc_src and
      'nohup waydroid shell sh "$A_S/$1"' in bc_src)
check("entrypoint delegates boot setup to boot-complete.sh",
      "sh /opt/boot-complete.sh" in ep_src)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
