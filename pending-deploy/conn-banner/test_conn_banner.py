#!/usr/bin/env python3
"""Tests for the dashboard grid connectivity banner (grid-conn-banner).

GAP: the grid poll (refresh(), every 10s) failed silently — a dropped tunnel
or wedged manager left stale cards on screen with zero indication.
manager/static/index.html now counts consecutive refresh failures; after
CONN_BANNER_AFTER (2) of them a red "Sin respuesta del servidor —
reintentando…" banner shows above the grid. One successful poll clears it.
401s skip the counter (logout() reloads the page anyway).

Covers (hermetic):
  A. Behavioral: extract the marked grid-conn-banner block from index.html,
     run it in node with a stubbed $/element, drive connNoteOk/connNoteFail
     and assert the hidden -> shown -> hidden transitions + text.
  B. Dashboard contract: connBanner element + CSS, wiring inside refresh()
     (connNoteOk on success, connNoteFail on failure, 401 skipped),
     threshold constant, Spanish label, no emojis.
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
    "[\U0001F000-\U0001FAFF\u2600-\u26FF\u2700-\u27BF\u2B00-\u2BFF\uFE0F]")


def main():
    print("conn-banner tests")
    try:
        src = open(INDEX, encoding="utf-8").read()
    except OSError as e:
        bad("read index.html", str(e))
        return 1

    # ---------- A. behavioral test of the extracted block in node ----------
    m = re.search(
        r"// == grid-conn-banner begin ==\n(.*?)\n// == grid-conn-banner end ==",
        src, re.S)
    check("conn-banner block has begin/end markers", m is not None)
    if m:
        driver = m.group(1) + r"""
;(function(){
  var results = [];
  function t(name, cond){ results.push((cond ? 'ok ' : 'FAIL ') + name); }
  // initial: counter zero, banner hidden
  t('initial connFails is 0', connFails === 0);
  t('initial banner hidden', el.classList.hidden === true);
  // one failure stays below the threshold
  connNoteFail();
  t('one fail still hidden', el.classList.hidden === true);
  t('one fail counts to 1', connFails === 1);
  // second consecutive failure shows the banner
  connNoteFail();
  t('two fails shows banner', el.classList.hidden === false);
  t('banner text carries the count', span.textContent === ' Sin respuesta del servidor \u2014 reintentando\u2026 (2)');
  // a third failure keeps it shown with the new count
  connNoteFail();
  t('three fails stays shown', el.classList.hidden === false);
  t('banner text updates count', /\(3\)$/.test(span.textContent));
  // one success clears everything
  connNoteOk();
  t('ok hides banner', el.classList.hidden === true);
  t('ok resets counter', connFails === 0);
  // failure after ok restarts from 1, still hidden
  connNoteFail();
  t('fail after ok still hidden', el.classList.hidden === true);
  t('fail after ok counts to 1', connFails === 1);
  // threshold constant is 2
  t('CONN_BANNER_AFTER is 2', CONN_BANNER_AFTER === 2);
  // missing element never throws
  $ = function(){ return null; };
  try { connNoteFail(); connNoteOk(); t('missing element never throws', true); }
  catch(e){ t('missing element never throws', false); }
  console.log(results.join('\n'));
})();
"""
        stub = (
            "var cls = {hidden: true, toggle: function(c, force){ this[c] = !!force; }};\n"
            "var span = {textContent: ''};\n"
            "var el = {classList: cls, querySelector: function(){ return span; }};\n"
            "function $(id){ return id === 'connBanner' ? el : null; }\n"
        )
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
    body = src

    # element: in the dash, right after the fleetbar, before the grid
    fm = re.search(r'<div class="fleetbar" id="fleetbar">.*?</div>'
                   r'<div id="connBanner" class="connbanner hidden">'
                   r'<i class="ri-wifi-off-line"></i><span></span></div>'
                   r'<div class="grid" id="grid">', body, re.S)
    check("connBanner element sits between fleetbar and grid", fm is not None)

    # CSS rule
    cm = re.search(r"\.connbanner \{[^}]*color:var\(--bad\)[^}]*"
                   r"border:1px solid var\(--bad\)", body)
    check(".connbanner CSS uses the --bad palette", cm is not None)

    # wiring inside refresh(): connNoteOk on success, connNoteFail in catch,
    # 401 ('unauthorized') skipped
    rm = re.search(r"async function refresh\(\) \{(.*?)\nasync function act\(",
                   body, re.S)
    check("refresh() body extractable", rm is not None)
    if rm:
        rbody = rm.group(1)
        check("refresh() calls connNoteOk() on the success path",
              "connNoteOk();" in rbody)
        check("refresh() catch calls connNoteFail()",
              "connNoteFail();" in rbody)
        check("refresh() catch skips the 401/unauthorized case",
              "e.message !== 'unauthorized'" in rbody)
        # the old silent comment is preserved (cards still keep old data)
        check("refresh() still keeps old cards on failure",
              "/* keep old */" in rbody)

    # block-level contracts
    if m:
        block = m.group(1)
        check("block declares CONN_BANNER_AFTER = 2",
              "const CONN_BANNER_AFTER = 2;" in block)
        check("block uses textContent (no innerHTML injection)",
              "textContent" in block and "innerHTML" not in block)
        check("block label is Spanish",
              "Sin respuesta del servidor" in block)
        check("block has no emojis", EMOJI_RE.search(block) is None)
        check("block hides via the hidden class",
              "classList.toggle('hidden'" in block)

    # the whole page still has no emoji where the banner text lives
    banner_line = [l for l in body.splitlines() if "Sin respuesta del servidor" in l]
    check("banner text line has no emojis",
          banner_line and all(EMOJI_RE.search(l) is None for l in banner_line))

    print(f"{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
