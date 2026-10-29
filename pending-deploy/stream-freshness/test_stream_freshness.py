#!/usr/bin/env python3
"""Tests for the dashboard stream-freshness indicator.

GAP: a stalled stream (dead screencap loop, wedged session) looked identical
to a live one — the last frame just sat there with an "En vivo" label.
manager/static/index.html now tracks every presented frame and shows
"Imagen congelada — sin cuadros desde hace Xs" in the viewer when no frame
arrived for >3s (frame hooks on all 3 transports, all decoder/display-side:
WebRTC rVFC/timeupdate, H.264 rVFC/timeupdate, MJPEG ws messages).

The H.264 ws.onmessage handler deliberately does NOT note frames: fragment
receipts cannot see a stalled MSE decoder (fragments can queue forever
while zero frames are presented), so the decoder-side hooks own freshness
there and a wedged decoder honestly shows the badge.

Covers (hermetic):
  A. Behavioral: extract the marked freshness block from index.html, run it
     in node with stubbed DOM/timers/clock, drive noteFrame/start/stop/tick
     and assert the element transitions (hidden -> stale text -> hidden).
  B. Dashboard contract: streamAge element + CSS, noteFrame wired into all
     three transports' frame paths (H.264: decoder-side only, never the ws
     handler), start/stop lifecycle in openViewer/closeViewer, threshold +
     Spanish label.
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
INDEX = os.path.join(REPO, "manager", "static", "index.html")

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


def block_between(src, start_anchor, end_anchor):
    i = src.find(start_anchor)
    if i < 0:
        return None
    j = src.find(end_anchor, i + len(start_anchor))
    if j < 0:
        return None
    return src[i:j]


def main():
    print("stream-freshness tests")
    try:
        src = open(INDEX, encoding="utf-8").read()
    except OSError as e:
        bad("read index.html", str(e))
        return 1

    # ---------- A. behavioral test of the extracted block in node ----------
    m = re.search(
        r"// == stream-freshness begin ==\n(.*?)\n// == stream-freshness end ==",
        src, re.S)
    check("freshness block has begin/end markers", m is not None)
    if m:
        driver = m.group(1) + r"""
;(function(){
  var results = [];
  function t(name, cond){ results.push((cond ? 'ok ' : 'FAIL ') + name); }
  function fire(){ for (var id in intervals) intervals[id].fn(); }
  // 1. start arms a single 1s interval and notes a frame
  streamFreshnessStart();
  t('start-notes-frame', lastFrameTs === now);
  var ids = Object.keys(intervals);
  t('start-arms-one-1s-interval', ids.length === 1 && intervals[ids[0]].ms === 1000);
  // 2. fresh tick hides the element
  now += 2000; fire();
  t('fresh-tick-hides', els.streamAge.style.display === 'none' && els.streamAge.textContent === '');
  // 3. stale tick shows the badge with age in seconds
  now += 1100; fire();
  t('stale-tick-shows', els.streamAge.style.display !== 'none');
  t('stale-tick-class', els.streamAge.className === 'hint stale');
  t('stale-tick-spanish-text', els.streamAge.textContent.indexOf('congelada') >= 0);
  t('stale-tick-age-seconds', els.streamAge.textContent.indexOf('3s') >= 0);
  // 4. a new frame recovers the badge
  noteFrame(); fire();
  t('recovery-hides', els.streamAge.style.display === 'none' && els.streamAge.textContent === '');
  // 5. start twice does not leak a second interval
  streamFreshnessStart();
  t('restart-single-interval', Object.keys(intervals).length === 1);
  // 6. stop clears the interval and the element
  var before = Object.keys(intervals).length;
  streamFreshnessStop();
  t('stop-clears-interval', before === 1 && Object.keys(intervals).length === 0);
  t('stop-hides-element', els.streamAge.style.display === 'none' && els.streamAge.textContent === '');
  // 7. threshold constant
  t('threshold-is-3000ms', FRESH_STALE_MS === 3000);
  console.log(results.join('\n'));
})();
"""
        harness = r"""
var fs = require('fs');
var INDEX = %s;
var src = fs.readFileSync(INDEX, 'utf8');
var m = src.match(/\/\/ == stream-freshness begin ==\n([\s\S]*?)\n\/\/ == stream-freshness end ==/);
if (!m) { console.log('FAIL harness-no-block'); process.exit(0); }
var now = 1000000;
var intervals = {}, nextId = 1;
function fakeEl(){ return {style: {display: 'none'}, className: 'hint', textContent: ''}; }
var els = { streamAge: fakeEl(), viewer: {classList: {contains: function(){ return true; }}} };
global.$ = function(id){ return els[id] || null; };
global.setInterval = function(fn, ms){ var id = nextId++; intervals[id] = {fn: fn, ms: ms}; return id; };
global.clearInterval = function(id){ delete intervals[id]; };
Date.now = function(){ return now; };
var driver = %s;
eval(driver);
""" % (repr(INDEX), repr(driver))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(harness)
            hpath = f.name
        try:
            r = subprocess.run(["node", hpath], capture_output=True, text=True,
                               timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            bad("node behavioral run", str(e))
            r = None
        finally:
            os.unlink(hpath)
        if r is not None:
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0:
                bad("node behavioral run exit 0", f"rc={r.returncode} out={out[:400]}")
            else:
                for line in out.splitlines():
                    line = line.strip()
                    if line.startswith("ok "):
                        ok("node:" + line[3:])
                    elif line.startswith("FAIL "):
                        bad("node:" + line[5:])

    # ---------- B. dashboard contract ----------
    check("streamAge element in viewer",
          'id="streamAge"' in src)
    check("stale CSS rule",
          re.search(r"#streamAge\.stale\s*\{", src) is not None)
    check("openViewer arms freshness",
          "streamFreshnessStart();" in
          block_between(src, "function openViewer(name){", "function startWebRTC") or "")
    check("closeViewer disarms freshness",
          "streamFreshnessStop();" in
          block_between(src, "function closeViewer(){", "$('btnCloseViewer').onclick") or "")
    mjpeg = block_between(src, "function startMJPEG(name){",
                          "// H.264-over-WebSocket") or ""
    check("MJPEG onmessage notes frame",
          "mjpegWs.onmessage" in mjpeg and "noteFrame();" in mjpeg)
    h264 = block_between(src, "function startH264(name){",
                         "function closeViewer(){") or ""
    h264_onmsg = block_between(h264, "ws.onmessage = ev =>{", "};") or ""
    check("H264 ws.onmessage does NOT note frame (decoder-side hooks own freshness)",
          "ws.onmessage" in h264 and "noteFrame" not in h264_onmsg,
          "fragment receipts cannot see a stalled MSE decoder")
    check("H264 hooks rVFC with viewer-open guard",
          "video.requestVideoFrameCallback" in h264 and
          "classList.contains('open')" in h264)
    check("H264 has timeupdate fallback for no-rVFC browsers",
          "video.ontimeupdate" in h264 and "noteFrame();" in
          block_between(h264, "} else {", "}\n  function pump") or "")
    webrtc = block_between(src, "async function startWebRTC(name){",
                           "// MJPEG fallback") or ""
    check("WebRTC ontrack notes frame",
          "pc.ontrack" in webrtc and "noteFrame();" in webrtc)
    check("WebRTC hooks rVFC with viewer-open guard",
          "requestVideoFrameCallback" in webrtc and
          "classList.contains('open')" in webrtc)
    check("spanish stale label",
          "Imagen congelada" in src)
    check("threshold constant 3000",
          "const FRESH_STALE_MS = 3000;" in src)
    check("freshness block declares noteFrame",
          "function noteFrame()" in src)
    check("streamAge hidden by default",
          'id="streamAge" style="display:none"' in src)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
