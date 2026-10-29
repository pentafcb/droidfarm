#!/usr/bin/env python3
"""Tests for the MJPEG terminal-retry guard (mjpeg-terminal-guard).

GAP: the MJPEG fallback's onclose handler retried every 3s forever,
unconditionally — including when the server rejects the socket BEFORE the
WebSocket upgrade (401 expired token, 404 deleted instance, 409 stopped
instance), where no retry can ever succeed. That is an infinite reconnect
loop hammering the manager while the viewer sits on a stale frame.
A socket that upgraded and THEN closed is different (frameless-guard
close, network blip): that is the designed recovery path and must keep
retrying. So startMJPEG now counts consecutive closes of sockets that
never opened; at MJPEG_MAX_NEVER_OPENED (3) it stops retrying and leaves
an honest terminal message in rbxState. One opened-then-closed socket
resets the streak; closeViewer() resets the counter for a fresh session.

Covers (hermetic):
  A. Behavioral: extract the marked mjpeg-terminal-guard block from
     index.html, run it in node with a stubbed $/rbxState element, and
     drive mjpegRetryAfterClose/mjpegResetGuard through streak, trip,
     reset, and missing-element sequences.
  B. Dashboard contract: startMJPEG wires onopen (mjpegOpened flag) and
     onclose through mjpegRetryAfterClose with the unchanged 3s retry,
     closeViewer resets the guard, the H.264 -> MJPEG fallback chain is
     intact, terminal text is Spanish, no emojis, textContent-only.
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


EMOJI_RE = re.compile(
    "[\\U0001F000-\\U0001FAFF\\u2600-\\u26FF\\u2700-\\u27BF\\u2B00-\\u2BFF\\uFE0F]")

TERMINAL_TEXT = (" No se pudo conectar a la transmisión — revisa la "
                 "instancia o vuelve a abrir el visor")


def main():
    print("mjpeg-terminal-guard tests")
    try:
        src = open(INDEX, encoding="utf-8").read()
    except OSError as e:
        bad("read index.html", str(e))
        return 1

    # ---------- A. behavioral test of the extracted block in node ----------
    m = re.search(
        r"// == mjpeg-terminal-guard begin ==\n(.*?)"
        r"\n// == mjpeg-terminal-guard end ==",
        src, re.S)
    check("mjpeg-terminal-guard block has begin/end markers", m is not None)
    if m:
        driver = m.group(1) + r"""
;(function(){
  var results = [];
  function t(name, cond){ results.push((cond ? 'ok ' : 'FAIL ') + name); }
  t('initial counter is 0', mjpegNeverOpened === 0);
  t('MJPEG_MAX_NEVER_OPENED is 3', MJPEG_MAX_NEVER_OPENED === 3);
  // two never-opened closes: still retrying, no terminal text yet
  t('first never-opened close retries', mjpegRetryAfterClose(false) === true);
  t('counter is 1', mjpegNeverOpened === 1);
  t('second never-opened close retries', mjpegRetryAfterClose(false) === true);
  t('counter is 2', mjpegNeverOpened === 2);
  t('no terminal text yet', rbState.textContent === '');
  // an opened-then-closed socket is transient: retry + streak reset
  t('opened close retries', mjpegRetryAfterClose(true) === true);
  t('opened close resets counter', mjpegNeverOpened === 0);
  // rebuild the streak to the threshold
  t('never-opened close retries (1)', mjpegRetryAfterClose(false) === true);
  t('counter is 1 again', mjpegNeverOpened === 1);
  t('never-opened close retries (2)', mjpegRetryAfterClose(false) === true);
  t('counter is 2 again', mjpegNeverOpened === 2);
  t('third never-opened close stops', mjpegRetryAfterClose(false) === false);
  t('counter is 3 at trip', mjpegNeverOpened === 3);
  t('terminal text set in Spanish', rbState.textContent === TERMINAL_TEXT);
  t('further never-opened close stays stopped', mjpegRetryAfterClose(false) === false);
  // fresh viewer session: reset restores retrying
  mjpegResetGuard();
  t('reset clears counter', mjpegNeverOpened === 0);
  t('never-opened close retries after reset', mjpegRetryAfterClose(false) === true);
  // missing element never throws, even on the terminal path
  $ = function(){ return null; };
  mjpegResetGuard();
  try {
    mjpegRetryAfterClose(false); mjpegRetryAfterClose(false);
    var stopped = mjpegRetryAfterClose(false);
    t('missing element never throws', stopped === false);
  } catch(e){ t('missing element never throws', false); }
  console.log(results.join('\n'));
})();
"""
        # The driver needs the exact terminal string; embed it as a JS
        # string literal via json to avoid quoting pitfalls.
        import json
        stub = ("var rbState = {textContent: ''};\n"
                "function $(id){ return id === 'rbxState' ? rbState : null; }\n"
                "var TERMINAL_TEXT = %s;\n" % json.dumps(TERMINAL_TEXT))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(stub + driver)
            path = f.name
        try:
            p = subprocess.run(["node", path], capture_output=True, text=True,
                               timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            bad("node driver run", str(e))
            p = None
        finally:
            os.unlink(path)
        if p is not None:
            check("node driver exited 0", p.returncode == 0, p.stderr[:200])
            for line in (p.stdout or "").splitlines():
                if line.startswith("ok "):
                    ok("node: " + line[3:])
                elif line.startswith("FAIL "):
                    bad("node: " + line[5:])

    # ---------- B. dashboard contract ----------
    sm = re.search(r"function startMJPEG\(name\)\{(.*?)\n// H\.264-over-WebSocket",
                   src, re.S)
    check("startMJPEG() body extractable", sm is not None)
    if sm:
        sbody = sm.group(1)
        check("startMJPEG tracks a per-socket opened flag",
              "let mjpegOpened = false;" in sbody)
        check("startMJPEG onopen sets the opened flag",
              "mjpegWs.onopen" in sbody and "mjpegOpened = true" in sbody)
        check("startMJPEG onclose routes through mjpegRetryAfterClose",
              "mjpegRetryAfterClose(mjpegOpened)" in sbody)
        check("onclose retry delay is still 3000ms",
              "startMJPEG(name); }, 3000" in sbody)
        check("startMJPEG still closes a previous socket first",
              "if (mjpegWs) { try{mjpegWs.close();}catch(e){} }" in sbody)
    check("closeViewer resets the guard for a fresh session",
          "mjpegResetGuard();" in src.split("function closeViewer()")[1]
          .split("}")[0] if "function closeViewer()" in src else False)
    check("h264Fail still falls back to startMJPEG (chain intact)",
          "startMJPEG(name);" in src)
    if m:
        block = m.group(1)
        check("block declares MJPEG_MAX_NEVER_OPENED = 3",
              "const MJPEG_MAX_NEVER_OPENED = 3;" in block)
        check("block uses textContent (no innerHTML injection)",
              "textContent" in block and "innerHTML" not in block)
        check("terminal text is Spanish",
              "No se pudo conectar a la transmisión" in block)
        check("block has no emojis", EMOJI_RE.search(block) is None)
    term_lines = [l for l in src.splitlines()
                  if "No se pudo conectar a la transmisión" in l]
    check("terminal text line has no emojis",
          term_lines and all(EMOJI_RE.search(l) is None for l in term_lines))

    print(f"PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
