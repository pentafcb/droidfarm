#!/usr/bin/env python3
"""wing-failfast: fail-fast wing_request on hot paths + last-known-good
remote-status cache for the dashboard poll.

Gap: wing_request() retried every failed connection 3x with 1s sleeps,
hard-coded. On the four hot paths that hit a wing node at high frequency
this turned a wedged node into a thread-pool attack on the manager:
  * GET /api/instances remote fetch (dashboard polls every 10s):
    up to ~17s serial block per provider (5s timeout x3 + 2x1s sleep),
    holding a BoundedHTTPServer handler thread per poll.
  * MJPEG _handle_ws per-frame screenshot fetch: up to ~32s per frame
    (10s x3), delaying the 15s frameless guard and pinning a handler
    thread per viewer.
  * H264 broadcaster frame_source tick: up to ~32s per tick, delaying the
    broadcaster's frameless guard.
  * WebRTC _get_webrtc_frame: up to ~32s per frame.
Fix:
  * wing_request(..., attempts=3): retry budget is now a parameter
    (default 3 = historical behavior, clamped to >=1).
  * The four hot paths pass attempts=1. Retry policy moves to the loop
    level where it already exists: 10s poll cadence, 3s MJPEG reconnect,
    H264 re-subscribe/broadcaster re-create, WebRTC recv loop.
  * User-initiated one-shots (input, launch-roblox, start/stop/restart,
    create, delete, login) keep the default 3 attempts.
  * NEW fetch_remote_statuses(): the /api/instances poll path fetches
    with attempts=1 and, on failure, reuses the last-known-good snapshot
    for 30s instead of flickering every remote card to "stopped".
    Best-effort, never raises.

Hermetic: no docker, no network (urllib + wing_request faked).
"""
import http.client
import inspect
import io
import os
import re
import socket
import sys
import time
import urllib.error
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


PROV = {"url": "http://127.0.0.1:9", "agent_key": "k"}


class FakeResp:
    def __init__(self, status=200, body=b'{"ok": true}', ctype="application/json"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._body


def boom():
    return http.client.RemoteDisconnected("boom")


# ------------------------------------------------- wing_request attempts ---
with mock.patch("urllib.request.urlopen") as uo, mock.patch("time.sleep") as sl:
    # 1. default: 3 attempts on RemoteDisconnected, 2 sleeps, 502
    uo.reset_mock(); sl.reset_mock()
    uo.side_effect = [boom(), boom(), boom()]
    code, resp = app.wing_request(PROV, "GET", "/x", timeout=1)
    check("default attempts=3: 3 urlopen calls", uo.call_count == 3)
    check("default attempts=3: 2 sleeps between attempts", sl.call_count == 2)
    check("default attempts=3: 502 node-unreachable",
          code == 502 and "node unreachable" in resp.get("error", ""))

    # 2. attempts=1: single call, no sleep, immediate 502
    uo.reset_mock(); sl.reset_mock()
    uo.side_effect = [boom()]
    code, resp = app.wing_request(PROV, "GET", "/x", timeout=1, attempts=1)
    check("attempts=1: exactly 1 urlopen call", uo.call_count == 1)
    check("attempts=1: no sleep", sl.call_count == 0)
    check("attempts=1: 502", code == 502)

    # 3. attempts=2: 2 calls, 1 sleep
    uo.reset_mock(); sl.reset_mock()
    uo.side_effect = [boom(), boom()]
    code, _ = app.wing_request(PROV, "GET", "/x", timeout=1, attempts=2)
    check("attempts=2: 2 calls + 1 sleep",
          uo.call_count == 2 and sl.call_count == 1 and code == 502)

    # 4/5. attempts clamped to >=1
    for bad in (0, -5):
        uo.reset_mock(); sl.reset_mock()
        uo.side_effect = [boom()]
        code, _ = app.wing_request(PROV, "GET", "/x", timeout=1, attempts=bad)
        check(f"attempts={bad}: clamped to 1 call, no sleep",
              uo.call_count == 1 and sl.call_count == 0 and code == 502)

    # 6. garbage attempts -> default 3
    uo.reset_mock(); sl.reset_mock()
    uo.side_effect = [boom(), boom(), boom()]
    code, _ = app.wing_request(PROV, "GET", "/x", timeout=1, attempts="bogus")
    check("attempts='bogus': falls back to 3 calls",
          uo.call_count == 3 and code == 502)

    # 7. retry still recovers: fail once, succeed on 2nd
    uo.reset_mock(); sl.reset_mock()
    uo.side_effect = [boom(), FakeResp(200, b'{"v": 1}')]
    code, resp = app.wing_request(PROV, "GET", "/x", timeout=1)
    check("retry recovers on 2nd attempt",
          code == 200 and resp == {"v": 1} and uo.call_count == 2
          and sl.call_count == 1)

    # 8. HTTPError: no retry regardless of attempts
    uo.reset_mock(); sl.reset_mock()
    uo.side_effect = urllib.error.HTTPError(
        "http://x/", 500, "err", {}, io.BytesIO(b'{"error":"bad"}'))
    code, resp = app.wing_request(PROV, "GET", "/x", timeout=1, attempts=3)
    check("HTTPError: single call, no sleep, code passthrough",
          uo.call_count == 1 and sl.call_count == 0
          and code == 500 and resp == {"error": "bad"})

    # 9. generic exception: immediate 502, no retry (documents behavior)
    uo.reset_mock(); sl.reset_mock()
    uo.side_effect = ValueError("weird")
    code, _ = app.wing_request(PROV, "GET", "/x", timeout=1, attempts=3)
    check("generic exception: 1 call, no retry",
          uo.call_count == 1 and sl.call_count == 0 and code == 502)

    # 10. success first try: JSON parsed
    uo.reset_mock(); sl.reset_mock()
    uo.side_effect = [FakeResp(200, b'{"a": [1,2]}')]
    code, resp = app.wing_request(PROV, "GET", "/x", timeout=1)
    check("success: 1 call, JSON body parsed",
          uo.call_count == 1 and code == 200 and resp == {"a": [1, 2]})

    # 11. non-JSON content type: raw bytes returned
    uo.reset_mock()
    uo.side_effect = [FakeResp(200, b"\xff\xd8jpeg", ctype="image/jpeg")]
    code, data = app.wing_request(PROV, "GET", "/shot", timeout=1)
    check("non-JSON: raw bytes passthrough",
          code == 200 and data == b"\xff\xd8jpeg")

# ------------------------------------------- fetch_remote_statuses ---
ST1 = {"name": "srt1", "running": True, "setup": "ready"}


def run_fetch(providers, fake):
    orig = app.wing_request
    app._remote_status_cache.clear()
    app.wing_request = fake
    try:
        return app.fetch_remote_statuses(providers)
    finally:
        app.wing_request = orig


def fresh_provider(pid="p1"):
    return {pid: {"url": "http://10.0.0.2:8090", "agent_key": "k",
                  "last_seen": time.time()}}


# 12. success populates map + cache
calls = []
out = run_fetch(fresh_provider(),
                lambda *a, **k: (calls.append((a, k)),
                                 (200, {"instances": [ST1]}))[1])
check("success: {(pid,name)} map", out == {("p1", "srt1"): ST1})
check("success: cache populated",
      app._remote_status_cache.get("p1", (0, {}))[1] == {("p1", "srt1"): ST1})
check("success: single fail-fast call with attempts=1",
      len(calls) == 1 and calls[0][1].get("attempts") == 1
      and calls[0][1].get("timeout") == 5)

# 13. stale last_seen: not even attempted
calls = []
out = run_fetch({"p1": {"url": "http://x", "agent_key": "k",
                        "last_seen": time.time() - 121}},
                lambda *a, **k: (calls.append(a), (200, {"instances": [ST1]}))[1])
check("stale last_seen (>120s): no fetch, empty", out == {} and not calls)

# 14. failure within TTL reuses last-good (no card flicker to stopped)
app._remote_status_cache.clear()
seq = [(200, {"instances": [ST1]}), (502, {"error": "down"})]
orig = app.wing_request
app.wing_request = lambda *a, **k: seq.pop(0)
try:
    first = app.fetch_remote_statuses(fresh_provider())
    second = app.fetch_remote_statuses(fresh_provider())
finally:
    app.wing_request = orig
check("failure within TTL: last-good reused",
      first == {("p1", "srt1"): ST1} and second == {("p1", "srt1"): ST1})

# 15. expired cache: honest empty
app._remote_status_cache.clear()
app._remote_status_cache["p1"] = (time.monotonic() - 31, {("p1", "srt1"): ST1})
orig = app.wing_request
app.wing_request = lambda *a, **k: (502, {"error": "down"})
try:
    out = app.fetch_remote_statuses(fresh_provider())
finally:
    app.wing_request = orig
check("expired cache (>30s): empty, no stale lie", out == {})

# 16. garbage providers never raise
for bad in (None, "x", 42, {"p1": "notadict"}, {"p1": None}):
    try:
        out = run_fetch(bad, lambda *a, **k: (200, {"instances": [ST1]}))
        ok = out == {}
    except Exception:
        ok = False
    check(f"garbage providers {bad!r}: empty, no raise", ok)

# 17. corrupt instances payload tolerated
out = run_fetch(fresh_provider(),
                lambda *a, **k: (200, {"instances": {"not": "a list"}}))
check("instances not a list: skipped, no raise", out == {})
out = run_fetch(fresh_provider(),
                lambda *a, **k: (200, {"instances": ["s", 42, {"name": 7},
                                                    {"name": "ok", "running": 1}]}))
check("bad entries filtered, good kept",
      out == {("p1", "ok"): {"name": "ok", "running": 1}})

# 18. wing_request raising outright never propagates
def raiser(*a, **k):
    raise RuntimeError("transport exploded")
try:
    out = run_fetch(fresh_provider(), raiser)
    ok = out == {}
except Exception:
    ok = False
check("wing_request raising: caught, empty", ok)

# 19. per-provider isolation: one fails (cached), one succeeds fresh
app._remote_status_cache.clear()
app._remote_status_cache["p1"] = (time.monotonic(),
                                  {("p1", "srt1"): ST1})
provs = dict(fresh_provider("p1"))
provs["p2"] = {"url": "http://10.0.0.3:8090", "agent_key": "k",
               "last_seen": time.time()}
ST2 = {"name": "srt2", "running": False}
def mixed(p, method, path, **kw):
    if "10.0.0.2" in p["url"]:
        return (502, {"error": "down"})
    return (200, {"instances": [ST2]})
orig = app.wing_request
app.wing_request = mixed
try:
    out = app.fetch_remote_statuses(provs)
finally:
    app.wing_request = orig
check("isolation: p1 cached + p2 fresh",
      out == {("p1", "srt1"): ST1, ("p2", "srt2"): ST2})

# 20. TTL boundary: just inside 30s still reused
# (uses 29.99 not 30.0: the check runs microseconds after the stamp)
app._remote_status_cache.clear()
app._remote_status_cache["p1"] = (time.monotonic() - 29.99,
                                  {("p1", "srt1"): ST1})
orig = app.wing_request
app.wing_request = lambda *a, **k: (502, {})
try:
    out = app.fetch_remote_statuses(fresh_provider())
finally:
    app.wing_request = orig
check("TTL boundary (30s): still reused", out == {("p1", "srt1"): ST1})

# 21. success with empty list overwrites stale cache (node answered: truly empty)
app._remote_status_cache.clear()
app._remote_status_cache["p1"] = (time.monotonic(), {("p1", "srt1"): ST1})
orig = app.wing_request
app.wing_request = lambda *a, **k: (200, {"instances": []})
try:
    out = app.fetch_remote_statuses(fresh_provider())
finally:
    app.wing_request = orig
check("fresh empty beats stale cache", out == {})

# 22. last_seen boundary: 119s fetched, 121s skipped
calls = []
def ok(*a, **k):
    calls.append(1)
    return (200, {"instances": []})
for age, want_calls in ((119, True), (121, False)):
    calls.clear()
    run_fetch({"p1": {"url": "http://x", "agent_key": "k",
                      "last_seen": time.time() - age}}, ok)
    check(f"last_seen {age}s: {'fetched' if want_calls else 'skipped'}",
          bool(calls) == want_calls)

# 23. missing last_seen defaults to 0 -> skipped
calls = []
run_fetch({"p1": {"url": "http://x", "agent_key": "k"}}, ok)
check("missing last_seen: skipped", not calls)

# ------------------------------------------------------ source contracts ---
SRC = os.path.join(os.path.dirname(__file__), "..", "..", "manager", "app.py")
src = open(SRC).read()

# 24. signature default preserved
check("wing_request signature keeps attempts=3 default",
      "def wing_request(provider, method, path, data=None, timeout=8, attempts=3):" in src)

# 25-28. the four hot paths fail fast.
# (Since remote-frame-cache, the three streaming paths fail fast through
# the shared get_remote_screenshot helper instead of inline: the contract
# is delegation + the helper's own attempts=1.)
_helper_src = inspect.getsource(app.get_remote_screenshot)
_helper_failfast = "timeout=10, attempts=1" in _helper_src
check("MJPEG _handle_ws get_frame: fail-fast via shared helper",
      _helper_failfast
      and "get_remote_screenshot(wing, prov, name)" in src)
check("H264 broadcaster frame_source: fail-fast via shared helper",
      _helper_failfast
      and src.count("get_remote_screenshot(wing, prov, name)") >= 2)
check("_get_webrtc_frame: fail-fast via shared helper",
      _helper_failfast
      and "get_remote_screenshot(wing, provider, name)" in src)
check("fetch_remote_statuses: timeout=5, attempts=1",
      re.search(r"def fetch_remote_statuses.*?timeout=5, attempts=1", src, re.S))

# 29. /api/instances uses the helper (old inline loop gone)
check("/api/instances calls fetch_remote_statuses(providers)",
      "remote_status = fetch_remote_statuses(providers)" in src)

# 30. user-initiated one-shots keep the default (no attempts= on their calls)
def call_window(needle, n=3):
    i = src.find(needle)
    if i < 0:
        return None
    return src[i:i + 400].split("\n")[:n]
user_calls = [
    ('f"/api/agent/instances/{name}/launch-roblox", timeout=30)', 1),
    ('f"/api/agent/instances/{name}/input",', 2),
    ('f"/api/agent/instances/{name}", timeout=60)', 1),  # DELETE
    ('f"/api/agent/instances/{name}/stop", timeout=60)', 2),  # batch stop
]
all_default = True
for needle, n in user_calls:
    w = call_window(needle, n)
    if w is None or any("attempts" in line for line in w):
        all_default = False
check("user one-shots (launch/input/delete/stop): no attempts= override",
      all_default)

# 31. TTL constant
check("_REMOTE_STATUS_TTL = 30", "_REMOTE_STATUS_TTL = 30" in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
